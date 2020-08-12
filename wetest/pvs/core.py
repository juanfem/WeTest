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

from wetest.common.constants import LVL_PV_DISCONNECTED, LVL_PV_CONNECTED
from wetest.common.constants import TERSE_FORMATTER, FILE_HANDLER
import p4p
from p4p.client.thread import Context
import epics
import time
import logging
from builtins import object
from builtins import str
from future import standard_library
standard_library.install_aliases()


# configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(TERSE_FORMATTER)
logger.addHandler(stream_handler)
logger.addHandler(FILE_HANDLER)


class PVData(object):
    """A class pikelable class to store PV connection status
    as well as trace all the subtests associated

    name:           full PV name
    setter_subtests:   a set of the subtest id using this PV as setter
    getter_subtests:   a set of the subtest id using this PV as getter
    subtested:         whether there is at least one subtest for this PV
    connected:      whether last check showed PV as connected
    """

    def __init__(self, name):
        self.name = name
        self.tests_titles = {}
        self.setter_subtests = set()
        self.getter_subtests = set()
        self.connected = None

    def __str__(self):
        output = "PV %s : " % self.name
        if self.connected is None:
            output += "connection not tested"
        else:
            output += "connected" if self.connected else "disconnected"

        if not self.tested:
            output += " -- not tested"
            return output

        if len(self.setter_subtests) > 0:
            output += " -- tested as setter (%s)" % len(self.setter_subtests)
        if len(self.getter_subtests) > 0:
            output += " -- tested as getter (%s)" % len(self.getter_subtests)
        output += "\n"+len(output)*"-"

        sc_id = -1
        for test_id in sorted(self.tests_titles, key=test_id_sort):
            new_sc_id = test_id.split("-")[1]
            if int(new_sc_id) != sc_id:
                sc_id = int(new_sc_id)
                output += "\nScenario " + new_sc_id
            output += "\n    %s" % (self.tests_titles[test_id])

        return output

    @property
    def tested(self):
        return len(self.setter_subtests) > 0 or len(self.getter_subtests) > 0


class PVConnection(object):
    """A convenience class to manage EPICS connections for PVInfo().

    connected:          whether last check showed PV as connected
    """

    @classmethod
    def get_pv_connection(cls, name, protocol, connection_callback=None):
        if protocol == 'CA':
            print('Using CA (d)')
            return cls.CaConnection(name, connection_callback)
        elif protocol == 'PVA':
            print('Using PVA (d)')
            return cls.PvaConnection(name, connection_callback)

    class CaConnection():
        def __init__(self, name, connection_callback):
            self.pvname = name
            self.pv = epics.PV(name, connection_callback=connection_callback)

        def check_connection(self):
            return self.pv.connect(timeout=0)

        @property
        def status(self):
            return self.pv.status

        def put(self, value):
            self.pv.put(value)

        def get(self):
            return self.pv.get()

    class PvaConnection():
        ctxt = Context('pva')

        def __init__(self, name, connection_callback):
            self.pvname = name
            self.connection_callback = connection_callback
            self.connected = False

            if self.connection_callback != None:
                self.monitor = self.ctxt.monitor(
                    name, self.connection_callback_wrapper)

        def check_connection(self):
            self.monitor.close()
            return self.connected

        def connection_callback_wrapper(self, value):
            self.connected = True
            self.connection_callback(pvname=self.pvname, conn=True)

        @property
        def status(self):
            return 1 if self.connected else 0

        def put(self, value):
            self.ctxt.put(self.pvname, value)

        def get(self):
            return self.ctxt.get(self.pvname)


class PVInfo(object):
    """A convenience class to manage PVData()

    name:           full PV name
    setter_subtests:   a set of the subtest id using this PV as setter
    getter_subtests:   a set of the subtest id using this PV as getter
    connected:          whether last check showed PV as connected
    """

    def __init__(self, name, protocol='PVA', setter_subtests=None, getter_subtests=None, connection_callback=None):

        self.data = PVData(name)

        if connection_callback != None:
            self.pv_connection = PVConnection.get_pv_connection(
                self.data.name, protocol, connection_callback)

        setter_subtests = set(
            [setter_subtests]) if setter_subtests is not None else set()
        getter_subtests = set(
            [getter_subtests]) if getter_subtests is not None else set()
        for subtest in setter_subtests | getter_subtests:
            self.add_subtest(subtest)

    @property
    def name(self):
        return self.data.name

    @property
    def setter_subtests(self):
        return self.data.setter_subtests

    @property
    def getter_subtests(self):
        return self.data.getter_subtests

    @property
    def connected(self):
        return self.data.connected

    @connected.setter
    def connected(self, value):
        self.data.connected = value

    @property
    def tested(self):
        return len(self.setter_subtests) > 0 or len(self.getter_subtests) > 0

    def check_connection(self):
        """If the PV accessible on the CA, update self.connected"""
        self.data.connected = self.pv_connection.check_connection()
        return self.data.connected

    def add_subtest(self, subtest_info):
        """Add subtest to PV getter_subtests or setter_subtests."""
        if self.data.name == subtest_info.setter:
            self.data.setter_subtests.add(subtest_info.id)
            self.add_title(subtest_info)

        if self.data.name == subtest_info.getter:
            self.data.getter_subtests.add(subtest_info.id)
            self.add_title(subtest_info)

    def add_title(self, subtest_info):
        """Add title in self.data.tests_titles"""
        test_id = subtest_info.id.split("_")[-1]
        title = subtest_info.test_title
        self.data.tests_titles[test_id] = title

    def remove_subtest(self, subtest):
        """Removes subtest from PV getter_subtests and setter_subtests.

        subtest may be a string (subtest_id) or a subtestData instance
        """
        if isinstance(subtest, str):  # id
            subtest_id = subtest
        elif hasattr(subtest_id, "id"):
            subtest_id = subtest.id
        else:
            NotImplementedError("Expecting subtest id or subtest info.")

        self.data.setter_subtests.discard(subtest_id)
        self.data.getter_subtests.discard(subtest_id)

        self.clean_titles()

    def clean_titles(self):
        """Remove test title if test not on PV anymore"""
        obsolete = []
        for test_id in self.data.tests_titles:
            found = False
            for subtest_id in self.data.setter_subtests | self.data.getter_subtests:
                if subtest_id.endswith(test_id):
                    found = True
                    break
            if not found:
                obsolete.append(test_id)

        if len(obsolete) >= 0:
            self.data.tests_titles = {
                k: v for k, v in list(self.data.tests_titles.items()) if k not in obsolete
            }

    def __str__(self):
        return str(self.data)


def test_id_sort(test_id):
    noise, sc_id, tt_id, st_id = test_id.split("-")
    return (int(sc_id), int(tt_id), int(st_id))


def pvs_from_suite(suite, ref_dict=None, connection_callback=None):
    """Determines all the PVs declared in suite"""
    if ref_dict is None:
        pvs_refs = dict()
    else:
        pvs_refs = ref_dict

    for test_data in list(suite.tests_infos.values()):
        # Set default protocol for the test
        PVConnection.default_protocol = test_data.protocol

        if test_data.setter is not None:
            if test_data.setter in pvs_refs:
                pvs_refs[test_data.setter].add_subtest(test_data)
            else:
                pvs_refs[test_data.setter] = PVInfo(
                    test_data.setter, test_data.protocol, setter_subtests=test_data,
                    connection_callback=connection_callback)

        if test_data.getter is not None:
            if test_data.getter in pvs_refs:
                pvs_refs[test_data.getter].add_subtest(test_data)
            else:
                pvs_refs[test_data.getter] = PVInfo(
                    test_data.getter, test_data.protocol, getter_subtests=test_data,
                    connection_callback=connection_callback)
    return pvs_refs


class PVsTable(object):
    """A class to reference PVs to be monitored,
    a queue can be provided to forward connection status with PV data"""

    def __init__(self, queue=None):
        if queue is None:
            import queue
            self.queue = queue.Queue()
        else:
            self.queue = queue
        self.pvs_refs = {}

    def register_pvs(self, suite=None, pv_list=None):
        """Check connection of all the PVs declared in suite"""
        if suite is None and pv_list is None:
            raise NotImplementedError(
                "Expecting pv_list or suite to be provided")
        # collect all the PVs and initialize the callback
        for pv_name in set(pv_list if pv_list is not None else []):
            self.pvs_refs[pv_name] = PVInfo(
                pv_name, connection_callback=self.connection_callback)

        if suite is not None:
            pvs_from_suite(
                suite,
                ref_dict=self.pvs_refs,
                connection_callback=self.connection_callback
            )

        all_connected = True

        # send PV status to GUI at least once per PV
        time.sleep(1)  # give some time to PV connection to settle

        for pv in list(self.pvs_refs.values()):
            # make sure that unreachable PV are displayed in stdout at least once
            if not pv.check_connection():
                all_connected = False
                logger.log(LVL_PV_DISCONNECTED,
                           "PV is unreachable: %s" % pv.name)
                self.queue.put(pv.data)

        return all_connected, self.pvs_refs

    def connection_callback(self, pvname=None, conn=None, **kws):
        """Updates PV status in pvs_refs and put data in queue"""
        if not conn:
            logger.log(LVL_PV_DISCONNECTED,
                       "PV changed to unreachable: %s" % pvname)
        else:
            logger.log(LVL_PV_CONNECTED,
                       "PV changed to connected: %s" % pvname)

        try:
            pv = self.pvs_refs[pvname]
            pv.connected = conn
            self.queue.put(pv.data)
        except KeyError:
            logger.critical(
                "connection_callback called on %s which is not referenced in PVsTable %s yet", pvname, self)
