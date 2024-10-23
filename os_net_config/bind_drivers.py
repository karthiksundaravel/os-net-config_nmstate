# -*- coding: utf-8 -*-

# Copyright 2024 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

#
# The bind_drivers.py module does the driver bindings required for DPDK
# devices. It'll be invoked by the NM dispatcher scripts
# An entry point os-net-config-bind is added for invocation of this module.

import argparse
import logging
import os
import sys

from os_net_config import common
from oslo_concurrency import processutils
logger = logging.getLogger(__name__)

def parse_opts(argv):

    parser = argparse.ArgumentParser(
        description='Configure SR-IOV PF and VF interfaces using a YAML'
        ' config file format.')

    parser.add_argument(
        '-d', '--debug',
        dest="debug",
        action='store_true',
        help="Print debugging output.",
        required=False)

    parser.add_argument(
        '-v', '--verbose',
        dest="verbose",
        action='store_true',
        help="Print verbose output.",
        required=False)

    parser.add_argument(
        '-p', '--pciaddress',
        dest="pci_address",
        action='store',
        help="Provide the pci address in the format Domain:Bus:Device.Function",
        required=True)

    parser.add_argument(
        '--driver',
        dest="driver",
        action='store',
        help="Provide the driver to override. When not specified unset-override",
        required=False)
    opts = parser.parse_args(argv[1:])

    return opts

def unset_driver_override(pci_address):

    cmd = ['driverctl', 'unset-override', pci_address ]
    logger.debug(f"{pci_address}: Running command: {cmd}")
    try:
        out, err = processutils.execute(*cmd)
        if err:
            msg = f'{pci_address}: Failed to unset override. err - {err}'
            raise common.OvsDpdkBindException(msg)
    except processutils.ProcessExecutionError:
        msg = f'{pci_address}: Failed to unset override.'
        raise common.OvsDpdkBindException(msg)

def set_driver_override(pci_address, driver):
    cmd = ['driverctl', '--nosave', 'set-override', pci_address, driver ]
    logger.debug(f"{pci_address}: Running command: {cmd}")
    try:
        out, err = processutils.execute(*cmd)
        if err:
            msg = f'{pci_address}: Failed to bind with {driver} err - {err}'
            raise common.OvsDpdkBindException(msg)
    except processutils.ProcessExecutionError:
        msg = f'{pci_address}: Failed to bind with {driver}'
        raise common.OvsDpdkBindException(msg)

def get_current_driver(pci_address):
    cmd = ['readlink', '-ve', f'/sys/bus/pci/devices/{pci_address}/driver']
    logger.debug(f"{pci_address}: Running command: {cmd}")
    try:
        out, err = processutils.execute(*cmd)
    except processutils.ProcessExecutionError:
        logger.error(f'{pci_address}: Failed to read driver')
    else:
        basename = os.path.basename(out)
        driver = basename.strip()
        logger.info(f'{pci_address}: attached to {driver}')
        return driver

def get_kernel_driver(pci_address):
    cmd = ['cat', f'/sys/bus/pci/devices/{pci_address}/modalias']
    logger.debug(f"{pci_address}: Running command: {cmd}")
    try:
        out, err = processutils.execute(*cmd)
    except processutils.ProcessExecutionError:
        logger.error(f'{pci_address}: Failed to read modalias')
        return None
    cmd = ['modprobe', '-R', out.strip()]
    try:
        out, err = processutils.execute(*cmd)
    except processutils.ProcessExecutionError:
        logger.error(f'{pci_address}: Failed to get the kernel driver')
    else:
        kernel_driver = out.strip()
        logger.info(f'{pci_address}: kernel driver is {kernel_driver}')
        return kernel_driver

def main(argv=sys.argv):
    opts = parse_opts(argv)
    logger = common.configure_logger(log_file=True)
    common.logger_level(logger, opts.verbose, opts.debug)
    common.set_noop(False)

    cur_driver = get_current_driver(opts.pci_address)
    if opts.driver and cur_driver == opts.driver:
        logger.info(f'{opts.pci_address}: Already bound with {opts.driver}')
    elif opts.driver:
        set_driver_override(opts.pci_address, opts.driver)
    else:
        kernel_driver = get_kernel_driver(opts.pci_address)
        if kernel_driver and kernel_driver == cur_driver:
            logger.info(f'{opts.pci_address}: Already bound with {cur_driver}. '
                        f'Hence skipping unset_override')
        else:
            unset_driver_override(opts.pci_address)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
