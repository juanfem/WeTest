import threading
import unittest
import logging
from queue import Queue

from wetest.testing.selectable_tests import SelectableTestResult
from wetest.report.generator import ReportGenerator

from wetest.common.constants import LVL_RUN_CONTROL

from wetest.common.constants import (
    SELECTION_FROM_GUI,
    START_FROM_GUI,
    RESUME_FROM_GUI,
    PAUSE_FROM_GUI,
    ABORT_FROM_GUI,
    END_OF_GUI,
    CONTINUE_FROM_TEST,
    PAUSE_FROM_TEST,
    ABORT_FROM_TEST,
    END_OF_TESTS,
    REPORT_GENERATED,
    PAUSE_FROM_MANAGER,
    ABORT_FROM_MANAGER,
    PLAY_FROM_MANAGER,
)


logger = logging.getLogger(__name__)


def quiet_exception(*args):
    """No traceback will be shown for the provided exceptions."""
    exception_tuple = tuple(args[:])

    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                func(*args, **kwargs)
            except exception_tuple:
                pass

        return wrapper

    return decorator


def export_pdf(filename, tests, results, configs, naming):
    """Export tests results to PDF file.

    :param filename: The PDF filename.
    :param tests:    The ran test case(s).
    :param results:  The test result(s).
    :param configs:   The report's suite and scenario configs.
    """
    logger.info("Results will be exported as PDF...")
    report = ReportGenerator(tests, results, filename, configs, naming)
    report.save()


class MultithreadedQueueStream(object):
    """Multiprocessing Queue implementing methodes of sys.stdout used by
    logging.StreamHandler

    Apparently there is no way to inherit from this object,
    so wrapping on it instead.
    """

    def __init__(self):
        self.queue = Queue()

    def put(self, data):
        self.queue.put(data)

    def get(self):
        return self.queue.get()

    def write(self, a_str=""):
        self.queue.put(a_str)

    def flush(self):
        self.write()


class ProcessManager(object):
    """Class that start/stop the runner and report process, and process their outputs."""

    def __init__(self, args, no_gui, queue_to_gui, queue_to_pm, queue_to_runner):
        self.queue_to_gui = queue_to_gui
        self.queue_to_pm = queue_to_pm
        self.queue_to_runner = queue_to_runner

        self.suite = args["suite"]
        self.pdf_output = args["pdf_output"]
        self.configs = args["configs"]
        self.naming = args["naming"]

        # trace start request  (to unpause run process)
        self.evt_start = threading.Event()
        self.evt_stop = threading.Event()

        # access to runner output
        self.runner_output = MultithreadedQueueStream()
        queue_handler = logging.StreamHandler(self.runner_output)
        queue_handler.setLevel(LVL_RUN_CONTROL)

        # process handles
        self.p_run_and_report = None
        self.p_gui_commands = None

        # process data sharing
        self.no_gui = no_gui
        # applaying test selection to suite needs to be done in runner process
        self.selection_from_GUI = threading.Event()
        self.selection = ()  # use an imutable type to ensure namespace update

        # results fill by test runner, used by report generator
        self.results = None

    def start_runner_process(self):
        """start runner in another process (also needs to be CA compatible)"""
        self.p_run_and_report = threading.Thread(
            target=self.run_and_report, name="run_and_report"
        )
        self.p_run_and_report.start()

    def start_gui_command_process(self):
        """sstart gui_commands in another process"""
        self.p_gui_commands = threading.Thread(
            target=self.gui_commands, name="gui_commands"
        )
        self.p_gui_commands.start()

    def run(self):
        """Start the various subprocess"""
        self.start_runner_process()

        if not self.no_gui:
            self.start_gui_command_process()

    def join(self):
        """Joins on the multiple process running"""
        self.p_run_and_report.join()
        if self.p_gui_commands is not None:
            self.p_gui_commands.join()

    def terminate(self):
        """Terminates the multiple process running"""
        self.queue_to_runner.put(ABORT_FROM_MANAGER)
        self.queue_to_gui.put(ABORT_FROM_MANAGER)
        self.evt_stop.set()
        self.evt_start.set()

    @quiet_exception(KeyboardInterrupt)
    def run_and_report(self):
        """Runs the tests and generate the report"""
        logger.debug("Enter run_and_report")
        logger.warning("-----------------------")
        if self.no_gui:
            logger.warning("Ready to start testing.")
        else:
            logger.warning("Ready to start testing, use GUI play button.")

        # do not start running before start requested
        self.evt_start.wait()
        self.evt_start.clear()

        # Stopping runner if execution was aborted.
        if self.evt_stop.isSet():
            self.evt_stop.clear()
            logger.debug("Stopping run_and_report")
            return

        if self.suite is not None:

            # update selection if necessary
            if self.selection_from_GUI.is_set():
                logger.info("Applying test selection...")
                selection = self.selection
                logger.debug("runner selected tests: %s", selection)
                self.update_selection(selected=selection)

            logger.info("Running tests suite...")

            self.runner = unittest.TextTestRunner(
                resultclass=SelectableTestResult, verbosity=0
            )  # use verbosity for debug

            # check that there are tests to run
            logger.info("Nbr tests: %d", self.suite.countTestCases())

            nbr_tests = len(self.suite._tests)
            if nbr_tests == 0:
                logger.error("No test to run.")
                self.results = []
            else:
                logger.info("Running %d tests...", nbr_tests)
                self.results = self.runner.run(self.suite)

            logger.info("Ran tests suite.")
            if self.results.shouldStop:
                return
            self.queue_to_pm.put(END_OF_TESTS)

        logger.warning("Done running tests.")

        # Generate PDF
        if self.results and self.pdf_output is not None:
            logger.info("Will export result in PDF file: %s", self.pdf_output)
            export_pdf(
                self.pdf_output, self.suite, self.results, self.configs, self.naming
            )
            logger.warning("Done generating report: %s", self.pdf_output)
            self.queue_to_gui.put(REPORT_GENERATED + " " + self.pdf_output)
        else:
            logger.warning("No report to generate.")

        logger.debug("Leave run_and_report")

    def pause_runner(self):
        self.queue_to_gui.put(PAUSE_FROM_MANAGER)
        self.queue_to_runner.put(PAUSE_FROM_MANAGER)
        if self.no_gui:
            logger.warning(
                "Pausing execution."
                + "\n  - To continue press Ctrl+Z and enter `fg`."
                + "\n  - To abort press Ctrl+C twice."
            )
        else:
            logger.warning(
                "Pausing execution."
                + "\n  - To continue, use GUI play button, or press Ctrl+Z and enter `fg`."
                + "\n  - To abort, use GUI abort button, or press Ctrl+C twice."
            )
        # os.kill(self.pid_run_and_report, signal.SIGSTOP)
        logger.debug("Paused run_and_report")

    def start_play(self):
        """Call play runner after setting evt_start"""
        # Empty queue from previous messages
        if not self.queue_to_runner.empty():
            self.queue_to_runner.get_nowait()
        if not self.queue_to_gui.empty():
            self.queue_to_gui.get_nowait()
        self.evt_start.set()
        self.play_runner()

    def resume_play(self):
        """Call play runner without setting evt_start"""
        self.play_runner()

    def play_runner(self):
        self.queue_to_gui.put(PLAY_FROM_MANAGER)
        self.queue_to_runner.put(PLAY_FROM_MANAGER)
        logger.info("Playing.")
        logger.debug("Continue run_and_report")

    @quiet_exception(KeyboardInterrupt)
    def gui_commands(self):
        """Process instructions from self.queue_to_pm"""
        logger.debug("Enter gui_commands")
        while True:
            cmd = self.queue_to_pm.get()
            logger.debug("command from gui: %s" % cmd)

            if str(cmd).startswith(SELECTION_FROM_GUI):
                # use an imutable type to ensure namespace update
                self.selection = tuple(cmd[len(SELECTION_FROM_GUI) + 1 :].split(" "))
                # logger.debug("gui commands selected tests: %s", self.ns.selection)
                self.selection_from_GUI.set()

            elif cmd == START_FROM_GUI:
                self.start_play()

            elif cmd == RESUME_FROM_GUI:
                self.resume_play()

            elif cmd == PAUSE_FROM_GUI:
                self.pause_runner()

            elif cmd == ABORT_FROM_GUI:
                self.terminate()
                self.start_runner_process()  # enable replay from GUI

            elif cmd == END_OF_GUI:
                self.terminate()
                return

            # check for test requested continue
            elif cmd == CONTINUE_FROM_TEST:
                logger.debug("=> Continue from test")
                pass

            # check for test requested pause
            elif cmd == PAUSE_FROM_TEST:
                logger.debug("=> Pause from test")
                self.pause_runner()

            # check for test requested abort
            elif cmd == ABORT_FROM_TEST:
                logger.debug("=> Abort from test")
                self.terminate()
                if self.no_gui:
                    break
                else:
                    self.start_runner_process()  # enable replay from GUI

            # check for no more tests
            elif cmd == END_OF_TESTS:
                logger.debug("=> No more test to run.")
                self.queue_to_gui.put(END_OF_TESTS)
                if self.no_gui:
                    break
                else:
                    self.start_runner_process()  # enable replay from GUI
            else:
                logger.critical("Unexpected gui command:\n%s" % cmd)

        logger.debug("Leave gui_commands")

    def update_selection(self, selected):
        """Select only the test provided in selected, otherwise skip them."""
        logger.warning("Applying selection may take some time.")
        self.suite.apply_selection(selected, "Skipped from GUI.")
