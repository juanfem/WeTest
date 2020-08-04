import time
import threading
import unittest
import os
import logging
import re
from queue import Queue

from wetest.testing.generator import PipedTextTestResult
from wetest.report.generator import ReportGenerator

from wetest.common.constants import LVL_RUN_CONTROL

from wetest.common.constants import (
    SELECTION_FROM_GUI, START_FROM_GUI, RESUME_FROM_GUI, PAUSE_FROM_GUI, ABORT_FROM_GUI,
    END_OF_GUI, CONTINUE_FROM_TEST, PAUSE_FROM_TEST, ABORT_FROM_TEST, END_OF_TESTS,
    REPORT_GENERATED, PAUSE_FROM_MANAGER, ABORT_FROM_MANAGER, PLAY_FROM_MANAGER
)

from wetest.gui.specific import (
    STATUS_UNKNOWN, STATUS_RUN, STATUS_RETRY, STATUS_SKIP,
    STATUS_ERROR, STATUS_FAIL, STATUS_SUCCESS
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

    def __init__(self, args, no_gui, queue_to_gui, queue_from_gui):
        self.queue_to_gui = queue_to_gui
        self.queue_from_gui = queue_from_gui

        self.suite = args["suite"]
        self.pdf_output = args["pdf_output"]
        self.configs = args["configs"]
        self.naming = args["naming"]

        # trace start request  (to unpause run process)
        self.evt_start = threading.Event()
        # stdin to subprocesses, for raw input use
        # https://stackoverflow.com/questions/13786974/raw-input-and-multiprocessing-in-python
        # self.stdin = os.fdopen(os.dup(sys.stdin.fileno()))

        # access to runner output
        self.runner_output = MultithreadedQueueStream()
        queue_handler = logging.StreamHandler(self.runner_output)
        queue_handler.setLevel(LVL_RUN_CONTROL)
        # logging.getLogger("_wetest_tests_results").addHandler(queue_handler)

        # process handles
        self.p_run_and_report = None
        # self.p_parse_output = None
        self.p_gui_commands = None

        # process status
        self.p_run_and_report_started = threading.Event()
        # self.p_parse_output_started = threading.Event()
        self.p_gui_commands_started = threading.Event()

        # process data sharing
        self.no_gui = no_gui
        # applaying test selection to suite needs to be done in runner process
        self.selection_from_GUI = threading.Event()
        self.selection = ()  # use an imutable type to ensure namespace update
        # store process pids for use by other processes
        # self.pid_run_and_report = None
        # self.pid_p_parse_output = None
        # self.pid_p_gui_commands = None

        # results fill by test runner, used by report generator
        self.results = None

    def start_runner_process(self):
        """start runner in another process (also needs to be CA compatible)"""
        self.p_run_and_report = threading.Thread(
            target=self.run_and_report, name="run_and_report")
        self.p_run_and_report.start()
        # self.pid_run_and_report = self.p_run_and_report.pid
        # logger.debug("pid_run_and_report: %s", self.ns.pid_run_and_report)
        self.p_run_and_report_started.wait()  # to be able to abort from p_parse_output

    # def start_parser_process(self):
    #     """start parse_output in another process"""
    #     self.p_parse_output = threading.Thread(
    #         target=self.parse_output, name="parse_output")
    #     self.p_parse_output.start()
    #     # self.pid_p_parse_output = self.p_parse_output.pid
    #     # logger.debug("pid_p_parse_output: %s", self.ns.pid_p_parse_output)
    #     # to be able to abort properly from p_gui_commands
    #     self.p_parse_output_started.wait()

    def start_gui_command_process(self):
        """sstart gui_commands in another process"""
        self.p_gui_commands = threading.Thread(
            target=self.gui_commands, name="gui_commands")
        # self.p_gui_commands.daemon = True # so that it can restart the runner and parser process
        self.p_gui_commands.start()
        # self.pid_p_gui_commands = self.p_gui_commands.pid
        # logger.debug("pid_p_gui_commands: %s", self.ns.pid_p_gui_commands)
        # for homogeneity with other process start functions
        self.p_gui_commands_started.wait()

    def run(self):
        """Start the various subprocess"""
        self.start_runner_process()
        # self.start_parser_process()

        if not self.no_gui:
            self.start_gui_command_process()

    def join(self):
        """Joins on the multiple process running"""
        self.p_run_and_report.join()
        # self.p_parse_output.join()
        if self.p_gui_commands is not None:
            self.p_gui_commands.join()

    def terminate(self):
        """Terminates the multiple process running"""
        self.p_run_and_report.terminate()
        # self.p_parse_output.terminate()
        # if self.p_gui_commands is not None:
        #     self.p_gui_commands.terminate()

    @quiet_exception(KeyboardInterrupt)
    def run_and_report(self):
        """Runs the tests and generate the report"""
        self.p_run_and_report_started.set()
        logger.debug("Enter run_and_report")
        logger.warning("-----------------------")
        if self.no_gui:
            logger.warning("Ready to start testing.")
        else:
            logger.warning("Ready to start testing, use GUI play button.")

        # do not start running before start requested
        self.evt_start.wait()
        self.evt_start.clear()

        if self.suite is not None:

            # update selection if necessary
            if self.selection_from_GUI.is_set():
                logger.info("Applying test selection...")
                selection = self.selection
                logger.debug("runner selected tests: %s", selection)
                self.update_selection(selected=selection)

            logger.info("Running tests suite...")

            self.runner = unittest.TextTestRunner(
                resultclass=PipedTextTestResult, verbosity=0)  # use verbosity for debug

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
            self.queue_to_gui.put(END_OF_TESTS)

        logger.warning("Done running tests.")

        # Generate PDF
        if self.results and self.pdf_output is not None:
            logger.info('Will export result in PDF file: %s', self.pdf_output)
            export_pdf(self.pdf_output, self.suite,
                       self.results, self.configs, self.naming)
            logger.warning("Done generating report: %s", self.pdf_output)
            self.queue_to_gui.put(REPORT_GENERATED + " " + self.pdf_output)
        else:
            logger.warning("No report to generate.")

        time.sleep(0.1)  # Just enough to let the Queue finish
        # https://stackoverflow.com/questions/36359528/broken-pipe-error-with-multiprocessing-queue.
        # TODO is this solved now that Queue comes from a multiprocessing.Manager ?

        logger.debug("Leave run_and_report")

    def pause_runner(self):
        self.queue_to_gui.put(PAUSE_FROM_MANAGER)
        if self.no_gui:
            logger.warning("Pausing execution."
                           + "\n  - To continue press Ctrl+Z and enter `fg`."
                           + "\n  - To abort press Ctrl+C twice.")
        else:
            logger.warning("Pausing execution."
                           + "\n  - To continue, use GUI play button, or press Ctrl+Z and enter `fg`."
                           + "\n  - To abort, use GUI abort button, or press Ctrl+C twice.")
        # os.kill(self.pid_run_and_report, signal.SIGSTOP)
        logger.debug("Paused run_and_report")

    def start_play(self):
        """Call play runner after setting evt_start"""
        self.evt_start.set()
        self.play_runner()

    def resume_play(self):
        """Call play runner without setting evt_start"""
        self.play_runner()

    def play_runner(self):
        self.queue_to_gui.put(PLAY_FROM_MANAGER)
        logger.info("Playing.")
        # os.kill(self.ns.pid_run_and_report, signal.SIGCONT)
        logger.debug("Continue run_and_report")

    def stop_runner(self):
        logger.warning("Aborting execution.")
        self.queue_to_gui.put(ABORT_FROM_MANAGER)  # notify GUI
        # os.kill(self.ns.pid_run_and_report, signal.SIGKILL) # actually stop tests
        logger.debug("Killed run_and_report")

    # def stop_parser(self):
    #     # os.kill(self.ns.pid_p_parse_output, signal.SIGKILL)
    #     logger.debug("Killed parse_output")

    @quiet_exception(KeyboardInterrupt)
    def gui_commands(self):
        """Process instructions from self.queue_from_gui"""
        self.p_gui_commands_started.set()
        logger.debug("Enter gui_commands")
        while True:
            cmd = self.queue_from_gui.get()
            logger.debug("command from gui: %s" % cmd)

            if str(cmd).startswith(SELECTION_FROM_GUI):
                # use an imutable type to ensure namespace update
                self.selection = tuple(
                    cmd[len(SELECTION_FROM_GUI)+1:].split(" "))
                # logger.debug("gui commands selected tests: %s", self.ns.selection)
                self.selection_from_GUI.set()

            elif cmd == START_FROM_GUI:
                self.start_play()

            elif cmd == RESUME_FROM_GUI:
                self.resume_play()

            elif cmd == PAUSE_FROM_GUI:
                self.pause_runner()

            elif cmd == ABORT_FROM_GUI:
                self.stop_runner()
                self.start_runner_process()  # enable replay from GUI

            elif cmd == END_OF_GUI:
                self.no_gui = True
                self.stop_runner()
                # self.stop_parser()
                return

            else:
                logger.critical("Unexpected gui command:\n%s" % cmd)

        logger.debug("Leave gui_commands")

    def update_selection(self, selected):
        """Select only the test provided in selected, otherwise skip them."""
        logger.warning("Applying selection may take some time.")
        self.suite.apply_selection(selected, "Skipped from GUI.")

    @quiet_exception(KeyboardInterrupt)
    def parse_output(self):
        """reads runner output and convert it to update data items
        in a new thread
        """
        self.p_parse_output_started.set()
        logger.debug("Enter parse_output")
        while True:

            # get next output to parse
            new_str = self.runner_output.get().rstrip("\n")
            logger.debug("from tests runner:>%s<" % new_str)
            # ignore empty lines
            if new_str == "":
                continue

            # check for start of test
            match_running = re.search(
                r"^Running\s*(?P<test_id>test-\d+-\d+-\d+)\s*.*$",
                new_str)
            # check for skipped test
            match_skipped = re.search(
                r"^Skipping\s*(?P<test_id>test-\d+-\d+-\d+)\s*.*$",
                new_str)
            # check for retry of test
            match_rerun = re.search(
                r"^Retry \(.*\)\s*(?P<test_id>test-\d+-\d+-\d+)\s*\(in (?P<duration>\d+\.\d+)s\)\s*(?P<trace>.*)$",
                new_str)
            # check for end of test
            match_result = re.match(
                r"^(?P<status>Success\s*of|Failure\s*of|Error\s*of)\s*(?P<test_id>test-\d+-\d+-\d+)\s*\(in (?P<duration>\d+\.\d+)s\)\s*(?P<trace>[\s\S]*)$",
                new_str)

            if match_running is not None:
                logger.debug("=> New test running")
                logger.debug("test_id: %s", match_running.group("test_id"))
                test_id = match_running.group("test_id")
                self.queue_to_gui.put([test_id, STATUS_RUN, None, None])

            elif match_skipped is not None:
                logger.debug("=> Test skipped")
                logger.debug("test_id: %s", match_skipped.group("test_id"))
                test_id = match_skipped.group("test_id")
                self.queue_to_gui.put([test_id, STATUS_SKIP, None, None])

            elif match_rerun is not None:
                logger.debug("=> Test retry")
                test_id = match_rerun.group("test_id")
                duration = match_rerun.group("duration")
                trace = match_rerun.group("trace")
                if trace == "":
                    trace = None
                self.queue_to_gui.put([test_id, STATUS_RETRY, duration, trace])

            elif match_result is not None:
                logger.debug("=> New test result")
                status = match_result.group("status")
                test_id = match_result.group("test_id")
                duration = match_result.group("duration")
                trace = match_result.group("trace")
                if trace == "":
                    trace = None
                if status.startswith("Error "):
                    status = STATUS_ERROR
                elif status.startswith("Failure "):
                    status = STATUS_FAIL
                elif status.startswith("Success "):
                    status = STATUS_SUCCESS
                else:
                    logger.error("Unexpected status "+status)
                    status = STATUS_UNKNOWN

                # finish running test
                self.queue_to_gui.put([test_id, status, duration, trace])

            # check for test requested continue
            elif new_str == CONTINUE_FROM_TEST:
                logger.debug("=> Continue from test")
                pass

            # check for test requested pause
            elif new_str == PAUSE_FROM_TEST:
                logger.debug("=> Pause from test")
                self.pause_runner()

            # check for test requested abort
            elif new_str == ABORT_FROM_TEST:
                logger.debug("=> Abort from test")
                self.stop_runner()
                if self.no_gui:
                    break
                else:
                    self.start_runner_process()  # enable replay from GUI

            # check for no more tests
            elif new_str == END_OF_TESTS:
                logger.debug("=> No more test to run.")
                self.queue_to_gui.put(END_OF_TESTS)
                if self.no_gui:
                    break
                else:
                    self.start_runner_process()  # enable replay from GUI

            # we should not reach here
            else:
                logger.critical("Unexpected test output:\n%s" % new_str)

        logger.debug("Leave parse_output")
