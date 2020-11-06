import unittest

from wetest.testing.reader import ABORT, PAUSE

from wetest.gui.specific import (
    STATUS_UNKNOWN,
    STATUS_RUN,
    STATUS_SKIP,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_SUCCESS,
)

from wetest.common.constants import PAUSE_FROM_TEST, ABORT_FROM_TEST
from wetest.common.constants import (
    PLAY_FROM_MANAGER,
    PAUSE_FROM_MANAGER,
    ABORT_FROM_MANAGER,
)

from wetest.testing.generator import skipped_test_factory


class SelectableTestResult(unittest.TextTestResult):
    """
    Extending TextTestResults to send
    """

    queue_to_gui = None
    queue_to_runner = None
    queue_to_pm = None

    def addSuccess(self, test):
        self.queue_to_gui.put([test._testMethodName, STATUS_RUN, None, None])
        super(SelectableTestResult, self).addSuccess(test)
        self.queue_to_gui.put(
            [
                test._testMethodName,
                STATUS_SUCCESS,
                test.test_data[test._testMethodName].elapsed,
                test.test_data[test._testMethodName].exception,
            ]
        )

    def addError(self, test, err):
        self.queue_to_gui.put([test._testMethodName, STATUS_RUN, None, None])
        super(SelectableTestResult, self).addError(test, err)
        self.queue_to_gui.put(
            [
                test._testMethodName,
                STATUS_ERROR,
                test.test_data[test._testMethodName].elapsed,
                test.test_data[test._testMethodName].exception,
            ]
        )
        self.handler_errors(test)

    def addFailure(self, test, err):
        self.queue_to_gui.put([test._testMethodName, STATUS_RUN, None, None])
        super(SelectableTestResult, self).addFailure(test, err)
        self.queue_to_gui.put(
            [
                test._testMethodName,
                STATUS_FAIL,
                test.test_data[test._testMethodName].elapsed,
                test.test_data[test._testMethodName].exception,
            ]
        )
        self.handler_errors(test)

    def addSkip(self, test, reason):
        self.queue_to_gui.put([test._testMethodName, STATUS_RUN, None, None])
        super(SelectableTestResult, self).addSkip(test, reason)
        self.queue_to_gui.put(
            [
                test._testMethodName,
                STATUS_SKIP,
                test.test_data[test._testMethodName].elapsed,
                test.test_data[test._testMethodName].exception,
            ]
        )

    def addExpectedFailure(self, test, err):
        self.queue_to_gui.put([test._testMethodName, STATUS_RUN, None, None])
        super(SelectableTestResult, self).addExpectedFailure(test, err)
        self.queue_to_gui.put(
            [
                test._testMethodName,
                STATUS_UNKNOWN,
                test.test_data[test._testMethodName].elapsed,
                test.test_data[test._testMethodName].exception,
            ]
        )
        self.handler_errors(test)

    def addUnexpectedSuccess(self, test):
        self.queue_to_gui.put([test._testMethodName, STATUS_RUN, None, None])
        super(SelectableTestResult, self).addUnexpectedSuccess(test)
        self.queue_to_gui.put(
            [
                test._testMethodName,
                STATUS_UNKNOWN,
                test.test_data[test._testMethodName].elapsed,
                test.test_data[test._testMethodName].exception,
            ]
        )
        self.handler_errors(test)

    def handler_errors(self, test):
        if test.test_data[test._testMethodName].on_failure == PAUSE:
            if not self.queue_to_runner.empty():
                self.queue_to_runner.get_nowait()
            self.queue_to_pm.put(PAUSE_FROM_TEST)
            cmd = self.queue_to_runner.get()
            while cmd == PAUSE_FROM_MANAGER:
                cmd = self.queue_to_runner.get()
            if cmd == PLAY_FROM_MANAGER:
                return
            elif cmd == ABORT_FROM_MANAGER:
                self.stop()
                return
        elif test.test_data[test._testMethodName].on_failure == ABORT:
            self.queue_to_pm.put(ABORT_FROM_TEST)
            self.stop()
            return


class SelectableTestCase(unittest.TestCase):
    """A unittest.TestCase to be filled with test methods, which can be skipped and unskipped"""

    test_data = {}
    func_backup = {}

    @classmethod
    def add_test(cls, test_data, func):
        """Adds a test method, mark the test as selected"""
        cls.test_data[test_data.id] = test_data
        cls.func_backup[test_data.id] = func
        cls.select(test_data.id)

    @classmethod
    def skip(cls, test_id, reason):
        """Skip the test method `test_id`"""
        setattr(cls, test_id, skipped_test_factory(cls.test_data[test_id], reason))

    @classmethod
    def select(cls, test_id):
        """Unskip the test method `test_id`"""
        setattr(cls, test_id, cls.func_backup[test_id])


class SelectableTestSuite(unittest.TestSuite):
    """A unittest.TestSuite with conveniency method to skip and unskip tests."""

    _cleanup = False

    def __init__(self, *args, **kargs):
        unittest.TestSuite.__init__(self, *args, **kargs)
        self._tests_data = {}
        self._skipped_tests = {}
        self._selected_tests = {}

    @property
    def tests_infos(self):
        """Keep test data available for later."""
        return self._tests_data

    def add_skipped_test(self, Test_case, test_id, reason):
        """Add a test to and its data to the suite and reference it as skipped."""
        self._skipped_tests[test_id] = Test_case
        Test_case.skip(test_id, reason)
        self.addTest(Test_case(test_id))
        self._tests_data[test_id] = Test_case.test_data[test_id]

    def add_selected_test(self, Test_case, test_id):
        """Add a test to and its data to the suite and reference it as selected."""
        self._selected_tests[test_id] = Test_case
        Test_case.select(test_id)
        self.addTest(Test_case(test_id))
        self._tests_data[test_id] = Test_case.test_data[test_id]

    def select(self, test_id):
        """Ensure test is selected"""
        if test_id in self._skipped_tests:
            test_case = self._skipped_tests.pop(test_id)
            test_case.select(test_id)
            self._selected_tests[test_id] = test_case

    def skip(self, test_id, reason):
        """Ensure test is skipped"""
        if test_id in self._selected_tests:
            test_case = self._selected_tests.pop(test_id)
            test_case.skip(test_id, reason)
            self._skipped_tests[test_id] = test_case

    def apply_selection(self, selection, reason):
        """Tests and skip tests, based on test ids in selection list."""
        already_selected = dict(self._selected_tests)
        already_skipped = dict(self._skipped_tests)
        for test_id in already_selected:
            if test_id not in selection:
                self.skip(test_id, reason)
        for test_id in selection:
            if test_id in already_skipped:
                self.select(test_id)
