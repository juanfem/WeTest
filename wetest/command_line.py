#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2019 by CEA
#
# The full license specifying the redistribution, modification, usage and other
# rights and obligations is included with the distribution of this project in
# the file "LICENSE".
#
# THIS SOFTWARE IS PROVIDED AS-IS WITHOUT WARRANTY OF ANY KIND, NOT EVEN THE
# IMPLIED WARRANTY OF MERCHANTABILITY. THE AUTHOR OF THIS SOFTWARE, ASSUMES
# _NO_ RESPONSIBILITY FOR ANY CONSEQUENCE RESULTING FROM THE USE, MODIFICATION,
# OR REDISTRIBUTION OF THIS SOFTWARE.

from wetest.testing.reader import (
    MacrosManager,
    ScenarioReader,
    FileNotFound,
    display_changlog,
)
from wetest.testing.selectable_tests import (
    SelectableTestSuite,
    SelectableTestResult,
)
from wetest.testing.generator import TestsGenerator
from wetest.pvs.db_parser import pvs_from_path
from wetest.pvs.naming import generate_naming, NamingError
from wetest.pvs.core import PVsTable

from wetest.gui.generator import GUIGenerator
from wetest.common.constants import TERSE_FORMATTER, FILE_HANDLER
from wetest.common.constants import LVL_FORMAT_VAL
from wetest.common.constants import END_OF_GUI
import tkinter as tk
import sys
import pkg_resources
import os
from queue import Queue
import argparse
import logging
from .process_manager import ProcessManager
from builtins import str
from future import standard_library

standard_library.install_aliases()

DESCRIPTION = """WeTest is a testing facility for EPICS modules. Tests are described in a
YAML file, and executed over the Channel Access using pyepics library.
A PDF report is generated with the tests results.
It also enables to monitor PVs (extracted from the tests and from specified DB).
"""


# logger setup
logger = logging.getLogger(__name__)
# logger.setLevel(logging.DEBUG)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(TERSE_FORMATTER)
logger.addHandler(stream_handler)
logger.addHandler(FILE_HANDLER)
# choose modules logging level
logging.getLogger("wetest.gui.generator").setLevel(logging.ERROR)
logging.getLogger("wetest.gui.specific").setLevel(logging.ERROR)
logging.getLogger("wetest.gui.base").setLevel(logging.ERROR)
logging.getLogger("wetest.report.generator").setLevel(logging.ERROR)
logging.getLogger("wetest.testing.generator").setLevel(logging.ERROR)
logging.getLogger("wetest.testing.reader").setLevel(logging.WARNING)

# Global constants
PREFIX = ""
DELAY = 1
FILE_PREFIX = "TEST-wetest.testing.generator.TestsSequence-"
OUTPUT_DIR = "/tmp/"


class ListStream(list):
    """List implementing methodes of sys.stdout used by logging.StreamHandler

    Apparently there is no way to inherit from this object,
    so wrapping on it instead.
    """

    def write(self, a_str=""):
        self.append(a_str)

    def flush(self):
        pass


def generate_tests(scenarios, macros_mgr=None, propagate=False):
    """Create a test suite from a YAML file (suite or scenario).

    :param scenario_file: A list of YAML scenario file path.
    :param macros_mgr:    MacrosManager with macros already defined

    :returns suite:       A unittest TestSuite object.
    :returns configs:     Scenarios config blocks.
    """
    suite = SelectableTestSuite()

    # get data from scenarios
    # read the first file
    tests_data = ScenarioReader(
        scenarios.pop(0), macros_mgr=macros_mgr, propagate=propagate
    ).get_deserialized()
    if "scenarios" not in tests_data:
        tests_data["scenarios"] = []
    # append scenario from remaining files
    for scenario in scenarios:
        new_tests_data = ScenarioReader(
            scenario, macros_mgr=macros_mgr, propagate=propagate
        ).get_deserialized()
        tests_data["scenarios"] += new_tests_data["scenarios"]

    # Get titles
    # Defaults title when several files from command line.
    configs = [{"name": "WeTest Suite"}]
    # Overwise get top title from first file
    if len(scenarios) == 0 and "name" in tests_data:
        configs = [{"name": tests_data["name"]}]
    # and populate TestSuite
    for idx, scenario in enumerate(tests_data["scenarios"]):
        logger.debug("Generate tests with TestGenerator...")
        tests_gen = TestsGenerator(scenario)
        configs.append(tests_gen.get_config())

        logger.debug("Append tests to suite...")
        tests_gen.append_to_suite(suite, scenario_index=idx)

    logger.debug("Created tests suite.")

    # display unit/functionnal info to user
    logger.warning(
        "Loaded %s tests from `%s`:", suite.countTestCases(), configs[0]["name"]
    )
    for scenario in tests_data["scenarios"]:
        if str(scenario["config"]["type"]).lower() == "unit":
            type_str = "unit tests (random)  "
        elif str(scenario["config"]["type"]).lower() == "functional":
            type_str = "functional (ordered) "
        else:
            type_str = str(scenario["config"]["type"]) + " (??)"
        logger.warning(
            "\t- %s `%s`", type_str, scenario["config"].get("name", "Unnamed")
        )

    return suite, configs


def main():
    """Program's main entry point."""
    logger.info("Launching WeTest...")

    # parse arguments
    parser = argparse.ArgumentParser(description=DESCRIPTION)

    parser.add_argument(
        "-V",
        "--version",
        action="count",
        default=0,
        help="Show WeTest version, also shows major changes is doubling the option",
    )

    # tests relative arguments
    parser.add_argument(
        "scenario_file",
        metavar="TEST_FILE",
        type=str,
        nargs="*",
        default=[],
        help="One or several scenario files (executed before scenarios from --scenario).",
    )
    parser.add_argument(
        "-s",
        "--scenario",
        metavar="TEST_FILE",
        type=str,
        nargs="+",
        default=[],
        help="One or several scenario files (executed after positional arguments).",
    )
    parser.add_argument(
        "-m",
        "--macros",
        metavar="MACRO=VALUE",
        type=str,
        nargs="+",
        action="append",
        help="Override macros defined in file.",
    )
    parser.add_argument(
        "--propagate-macros",
        action="store_true",
        default=False,
        help="Macros defined in a file are given to included files. Default behavior is that only macros set on the include line are given to the included file.",
    )

    # PVs relative arguments
    pvs_group = parser.add_mutually_exclusive_group(required=False)
    pvs_group.add_argument(
        "-d",
        "--db",
        metavar="DB_PATH",
        type=str,
        nargs="+",
        default=[],
        help="EPICS DB files and directory to extract additional PVs from.",
    )
    pvs_group.add_argument(
        "-D",
        "--no-pv",
        action="store_true",
        default=False,
        help="Run withtout monitoring any PVs.",
    )
    parser.add_argument(
        "-n",
        "--naming",
        type=str,
        default="None",
        choices=["ESS", "RDS-81346", "SARAF", "None"],
        help="Specifies naming convention to display PV name (defaults to None).",
    )

    # run relative arguments
    parser.add_argument(
        "-G", "--no-gui", action="store_true", default=False, help="Do not open a GUI."
    )
    auto_play_group = parser.add_mutually_exclusive_group(required=False)
    auto_play_group.add_argument(
        "-p",
        "--force-play",
        action="store_true",
        default=False,
        help="Start runnning tests automatically even with disconnected PV.",
    )
    auto_play_group.add_argument(
        "-P",
        "--no-auto-play",
        action="store_true",
        default=False,
        help="Tests will not start running automatically.",
    )

    # output relative arguments
    report_group = parser.add_mutually_exclusive_group(required=False)
    report_group.add_argument(
        "-o",
        "--pdf-output",
        metavar="OUTPUT_FILE",
        type=str,
        default="wetest-results.pdf",
        help="Specify PDF output file name (otherwise defaults to wetest-results.pdf).",
    )
    report_group.add_argument(
        "-O",
        "--no-pdf-output",
        action="store_true",
        default=False,
        help="Do not generate the PDF report with tests results.",
    )

    args = parser.parse_args()

    logger.info("Processing arguments...")

    version = pkg_resources.require("WeTest")[0].version
    if args.version:
        major, minor, bugfix = [int(x) for x in version.split(".")]
        logger.warning("Installed WeTest is of version %d.%d.%d", major, minor, bugfix)
        if args.version > 1:
            display_changlog((major, 0, 0), (major, minor, bugfix))
        sys.exit(1)

    with_gui = not args.no_gui

    scenarios = args.scenario_file + args.scenario
    # Check parameters are valid
    if len(scenarios) == 0 and len(args.db) == 0:
        parser.print_usage()
        logger.error(
            "A test scenario (--scenario) or a directory from which to extact PVs (--db) is required"
        )
        sys.exit(2)

    # select naming convention
    naming = generate_naming(args.naming)

    # get PVs from DB files
    pvs_from_db = []
    if args.db and not args.no_pv:
        pvs_from_db = pvs_from_path(args.db)
    pvs_from_files = [pv["name"] for pv in pvs_from_db]

    # deal with CLI macros
    cli_macros = {}
    if args.macros:
        # we get a list of list because we enable "append" action mode
        for macros_list in args.macros:
            for macro in macros_list:
                try:
                    k, v = macro.split("=", 1)
                    if k in cli_macros:
                        logger.error(
                            "`%s` already defined in CLI, using value: %s",
                            k,
                            cli_macros[k],
                        )
                    else:
                        cli_macros[k] = v
                except ValueError:
                    logger.critical("Could not parse a MACRO=VALUE in %s", macro)
                    raise
        logger.info(
            "using CLI macros:\n%s",
            "\n".join(["\t%s: %s" % (k, v) for k, v in list(cli_macros.items())]),
        )
    macros_mgr = MacrosManager(known_macros=cli_macros)

    # file validation logging
    fv_list = ListStream()
    fv_handler = logging.StreamHandler(fv_list)
    fv_handler.setLevel(LVL_FORMAT_VAL)
    logging.getLogger("_wetest_format_validation").addHandler(fv_handler)

    # generate tests from file
    suite, configs = None, [{"name": "No tests to run"}]
    if len(scenarios) != 0:
        logger.info("Will load tests from files:\n\t-%s", "\n\t-".join(scenarios))
        try:
            suite, configs = generate_tests(
                scenarios=scenarios,
                macros_mgr=macros_mgr,
                propagate=args.propagate_macros,
            )
        except FileNotFound as e:
            logger.error(e)
            exit(4)

    # stop here if no PVs to monitor and no test to run
    if len(pvs_from_files) == 0 and suite.countTestCases() == 0:
        logger.error("Please provide at least a test to run or PVs to monitor.")
        sys.exit(3)

    queue_to_gui = Queue()
    queue_to_pm = Queue()
    queue_to_runner = Queue()
    SelectableTestResult.queue_to_gui = queue_to_gui
    SelectableTestResult.queue_to_pm = queue_to_pm
    SelectableTestResult.queue_to_runner = queue_to_runner

    # monitor PVs
    if args.no_pv:
        all_connected, pv_refs = True, {}
    else:
        all_connected, pv_refs = PVsTable(queue_to_gui).register_pvs(
            pv_list=pvs_from_files, suite=suite
        )

    # show naming compatibility in CLI
    for pv_name, pv in list(pv_refs.items()):
        try:
            naming.split(pv_name)
        except NamingError as e:
            logger.error(e)

    # decide whether to run tests or not
    autoplay = (all_connected or args.force_play) and not args.no_auto_play
    if args.no_auto_play:
        logger.error("Not starting tests as required.")
    else:
        if not all_connected:
            if not args.force_play:
                logger.error(
                    "Not starting tests as some PVs are currently not reachable."
                )
            else:
                logger.error(
                    "Starting tests even though some PVs are currently not reachable."
                )

    # generate GUI
    if with_gui:

        logger.info("Opening GUI...")
        root = tk.Tk()
        GUIGenerator(
            master=root,
            suite=suite,
            configs=configs,
            naming=naming,
            update_queue=queue_to_gui,
            request_queue=queue_to_pm,
            file_validation=fv_list,
        )

    if args.no_pdf_output:
        pdf_output = None
    else:
        pdf_output = os.path.abspath(args.pdf_output)

    # run tests
    data = {
        "update_queue": queue_to_gui,
        "suite": suite,
        "configs": configs,
        "pdf_output": pdf_output,
        "naming": naming,
    }

    pm = ProcessManager(data, not with_gui, queue_to_gui, queue_to_pm, queue_to_runner)
    try:

        pm.run()

        if autoplay:
            pm.start_play()
        else:
            logger.warning("Waiting for user.")
            if not with_gui:  # no GUI with a play button
                logger.warning(
                    "  - To start testing, press Ctrl+D then ENTER.\n"
                    + "  - To abort, press Ctrl+C."
                )
                sys.stdin.readlines()  # to flush previous inputs, requires Ctrl+D
                sys.stdin.readline()  # ENTER to return from this one
                pm.start_play()
            else:
                logger.warning(
                    "  - To start testing, use GUI play button.\n"
                    + "  - To abort, use GUI abort button, or press Ctrl+C."
                )

        if with_gui:
            root.mainloop()
            logger.warning("GUI closed.")
            queue_to_pm.put(END_OF_GUI)

        pm.join()
    except (KeyboardInterrupt, SystemExit):
        pm.terminate()
        logger.error("Aborting WeTest.")
    else:
        # clean ending
        logger.warning("Exiting WeTest.")


if __name__ == "__main__":
    main()
