# -*- coding: utf-8 -*-

# Copyright 2014 Red Hat, Inc.
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
# The sriov_config.py module does the SR-IOV PF configuration.
# It'll be invoked by the sriov_config systemd service for the persistence of
# the SR-IOV configuration across reboots. And os-net-config:utils also invokes
# it for the first time configuration.
# An entry point os-net-config-sriov is added for invocation of this module.

import argparse
import logging
import os
import pyudev
import re
from six.moves import queue as Queue
import sys
import time
import yaml

from oslo_concurrency import processutils

logger = logging.getLogger(__name__)
_SYS_CLASS_NET = '/sys/class/net'
_UDEV_RULE_FILE = '/etc/udev/rules.d/80-persistent-os-net-config.rules'
_UDEV_LEGACY_RULE_FILE = '/etc/udev/rules.d/70-os-net-config-sriov.rules'
_IFUP_LOCAL_FILE = '/sbin/ifup-local'
_RESET_SRIOV_RULES_FILE = '/etc/udev/rules.d/70-tripleo-reset-sriov.rules'
_ALLOCATE_VFS_FILE = '/etc/sysconfig/allocate_vfs'

MAX_RETRIES = 10
PF_FUNC_RE = re.compile(r"\.(\d+)$", 0)
# In order to keep VF representor name consistent specially after the upgrade
# proccess, we should have a udev rule to handle that.
# The udev rule will rename the VF representor as "<sriov_pf_name>_<vf_num>"
_REP_LINK_NAME_FILE = "/etc/udev/rep-link-name.sh"
_REP_LINK_NAME_DATA = '''#!/bin/bash
# This file is autogenerated by os-net-config
set -x
PORT="$1"
echo "NUMBER=${PORT##pf*vf}"
'''

# Create a queue for passing the udev network events
vf_queue = Queue.Queue()


# File to contain the list of SR-IOV PF, VF and their configurations
# Format of the file shall be
# - device_type: pf
#   name: <pf name>
#   numvfs: <number of VFs>
#   promisc: "on"/"off"
# - device_type: vf
#   device:
#      name: <pf name>
#      vfid: <VF id>
#   name: <vf name>
#   vlan_id: <vlan>
#   qos: <qos>
#   spoofcheck: "on"/"off"
#   trust: "on"/"off"
#   state: "auto"/"enable"/"disable"
#   macaddr: <mac address>
#   promisc: "on"/"off"
_SRIOV_CONFIG_FILE = '/var/lib/os-net-config/sriov_config.yaml'


class SRIOVNumvfsException(ValueError):
    pass


def udev_event_handler(action, device):
    event = {"action": action, "device": device.sys_path}
    logger.info("Received udev event %s for %s"
                % (event["action"], event["device"]))
    vf_queue.put(event)


def get_file_data(filename):
    if not os.path.exists(filename):
        return ''
    try:
        with open(filename, 'r') as f:
            return f.read()
    except IOError:
        logger.error("Error reading file: %s" % filename)
        return ''


def _get_sriov_map():
    contents = get_file_data(_SRIOV_CONFIG_FILE)
    sriov_map = yaml.safe_load(contents) if contents else []
    return sriov_map


def get_numvfs(ifname):
    try:
        sriov_numvfs_path = os.path.join(_SYS_CLASS_NET, ifname,
                                         "device/sriov_numvfs")
        with open(sriov_numvfs_path, 'r') as f:
            return int(f.read())
    except IOError:
        msg = ("Unable to read numvfs for %s" % ifname)
        raise SRIOVNumvfsException(msg)


def restart_ovs_and_pfs_netdevs():
    sriov_map = _get_sriov_map()
    processutils.execute('/usr/bin/systemctl', 'restart', 'openvswitch')
    for item in sriov_map:
        if item['device_type'] == 'pf':
            if_down_interface(item['name'])
            if_up_interface(item['name'])


def cleanup_puppet_config():
    file_contents = ""
    if os.path.exists(_RESET_SRIOV_RULES_FILE):
        os.remove(_RESET_SRIOV_RULES_FILE)
    if os.path.exists(_ALLOCATE_VFS_FILE):
        os.remove(_ALLOCATE_VFS_FILE)
    if os.path.exists(_IFUP_LOCAL_FILE):
        # Remove the invocation of allocate_vfs script generated by puppet
        # After the removal of allocate_vfs, if the ifup-local file has just
        # "#!/bin/bash" left, then remove the file as well.
        with open(_IFUP_LOCAL_FILE) as oldfile:
            for line in oldfile:
                if "/etc/sysconfig/allocate_vfs" not in line:
                    file_contents = file_contents + line
        if file_contents.strip() == "#!/bin/bash":
            os.remove(_IFUP_LOCAL_FILE)
        else:
            with open(_IFUP_LOCAL_FILE, 'w') as newfile:
                newfile.write(file_contents)


def udev_monitor_setup():
    # Create a context for pyudev and observe udev events for network
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by('net')
    observer = pyudev.MonitorObserver(monitor, udev_event_handler)
    return observer


def udev_monitor_start(observer):
    observer.start()


def udev_monitor_stop(observer):
    observer.stop()


def configure_sriov_pf(execution_from_cli=False, restart_openvswitch=False):
    observer = udev_monitor_setup()
    udev_monitor_start(observer)

    sriov_map = _get_sriov_map()
    MLNX_UNBIND_FILE_PATH = "/sys/bus/pci/drivers/mlx5_core/unbind"
    MLNX_VENDOR_ID = "0x15b3"
    trigger_udev_rule = False

    # Cleanup the previous config by puppet-tripleo
    cleanup_puppet_config()

    for item in sriov_map:
        if item['device_type'] == 'pf':
            _pf_interface_up(item)
            if item.get('link_mode') == "legacy":
                # Add a udev rule to configure the VF's when PF's are
                # released by a guest
                add_udev_rule_for_legacy_sriov_pf(item['name'],
                                                  item['numvfs'])
            try:
                sriov_numvfs_path = os.path.join(_SYS_CLASS_NET, item['name'],
                                                 "device/sriov_numvfs")
                curr_numvfs = get_numvfs(item['name'])
                if curr_numvfs == item['numvfs']:
                    logger.info("Numvfs already configured for %s"
                                % item['name'])
                    continue
                with open(sriov_numvfs_path, 'w') as f:
                    f.write("%d" % item['numvfs'])
            except IOError as exc:
                msg = ("Unable to configure pf: %s with numvfs: %d\n%s"
                       % (item['name'], item['numvfs'], exc))
                raise SRIOVNumvfsException(msg)
            # Wait for the creation of VFs for each PF
            _wait_for_vf_creation(item['name'], item['numvfs'])
            # Configure switchdev mode
            vendor_id = get_vendor_id(item['name'])
            if (item.get('link_mode') == "switchdev" and
                    vendor_id == MLNX_VENDOR_ID):
                vf_pcis_list = get_vf_pcis_list(item['name'])
                for vf_pci in vf_pcis_list:
                    vf_pci_path = "/sys/bus/pci/devices/%s/driver" % vf_pci
                    if os.path.exists(vf_pci_path):
                        with open(MLNX_UNBIND_FILE_PATH, 'w') as f:
                            f.write("%s" % vf_pci)

                # Adding a udev rule to make vf-representors unmanaged by
                # NetworkManager
                add_udev_rule_to_unmanage_vf_representors_by_nm()

                # Adding a udev rule to save the sriov_pf name
                trigger_udev_rule = add_udev_rule_for_sriov_pf(item['name'])\
                    or trigger_udev_rule

                configure_switchdev(item['name'])

                # Adding a udev rule to rename vf-representors
                trigger_udev_rule = add_udev_rule_for_vf_representors(
                    item['name']) or trigger_udev_rule

                # Moving the sriov-PFs to switchdev mode will put the netdev
                # interfaces in down state.
                # In case we are running during initial deployment,
                # bring the interfaces up.
                # In case we are running as part of the sriov_config service
                # after reboot, net config scripts, which run after
                # sriov_config service will bring the interfaces up.
                if execution_from_cli:
                    if_up_interface(item['name'])

    # Trigger udev rules if there is new rules written
    if trigger_udev_rule:
        trigger_udev_rules()

    udev_monitor_stop(observer)
    if restart_openvswitch:
        restart_ovs_and_pfs_netdevs()


def _write_numvfs(device_name, numvfs):

    sriov_numvfs_path = os.path.join(_SYS_CLASS_NET, device_name,
                                     "device/sriov_numvfs")
    curr_numvfs = get_numvfs(device_name)
    if curr_numvfs != 0:
        logger.info("Numvfs already configured for %s" % device_name)
        return
    try:
        with open(sriov_numvfs_path, 'w') as f:
            f.write("%d" % numvfs)
    except IOError as exc:
        msg = ("Unable to configure pf: %s with numvfs: %d\n%s"
               % (device_name, numvfs, exc))
        raise SRIOVNumvfsException(msg)


def _wait_for_vf_creation(pf_name, numvfs):
    vf_count = 0
    vf_list = []
    while vf_count < numvfs:
        try:
            # wait for 5 seconds after every udev event
            event = vf_queue.get(True, 5)
            vf_name = os.path.basename(event["device"])
            pf_path = os.path.normpath(os.path.join(event["device"],
                                                    "../../physfn/net"))
            if os.path.isdir(pf_path):
                pf_nic = os.listdir(pf_path)
                if len(pf_nic) == 1 and pf_name == pf_nic[0]:
                    if vf_name not in vf_list:
                        vf_list.append(vf_name)
                        logger.info("VF: %s created for PF: %s"
                                    % (vf_name, pf_name))
                        vf_count = vf_count + 1
                else:
                    logger.warning("Unable to parse event %s"
                                   % event["device"])
            else:
                logger.warning("%s is not a directory" % pf_path)
        except Queue.Empty:
            logger.info("Timeout in the creation of VFs for PF %s" % pf_name)
            return
    logger.info("Required VFs are created for PF %s" % pf_name)


def _wait_for_uplink_rep_creation(pf_name):
    uplink_rep_phys_switch_id_path = "/sys/class/net/%s/phys_switch_id" \
                                     % pf_name

    for i in range(MAX_RETRIES):
        if get_file_data(uplink_rep_phys_switch_id_path):
            logger.info("Uplink representor %s ready", pf_name)
            break
        time.sleep(1)
    else:
        raise RuntimeError("Timeout while waiting for uplink representor %s.",
                           pf_name)


def create_rep_link_name_script():
    with open(_REP_LINK_NAME_FILE, "w") as f:
        f.write(_REP_LINK_NAME_DATA)
    # Make the _REP_LINK_NAME_FILE executable
    os.chmod(_REP_LINK_NAME_FILE, 0o755)


def add_udev_rule_for_sriov_pf(pf_name):
    pf_pci = get_pf_pci(pf_name)
    udev_data_line = 'SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*", '\
                     'KERNELS=="%s", NAME="%s"' % (pf_pci, pf_name)
    return add_udev_rule(udev_data_line, _UDEV_RULE_FILE)


def add_udev_rule_for_legacy_sriov_pf(pf_name, numvfs):
    logger.info("adding udev rules for %s" % (pf_name))
    udev_line = 'KERNEL=="%s", '\
                'RUN+="/bin/os-net-config-sriov -n %%k:%d"' \
                % (pf_name, numvfs)
    return add_udev_rule(udev_line, _UDEV_LEGACY_RULE_FILE)


def add_udev_rule_for_vf_representors(pf_name):
    phys_switch_id_path = os.path.join(_SYS_CLASS_NET, pf_name,
                                       "phys_switch_id")
    phys_switch_id = get_file_data(phys_switch_id_path).strip()
    pf_pci = get_pf_pci(pf_name)
    pf_fun_num_match = PF_FUNC_RE.search(pf_pci)
    if pf_fun_num_match:
        pf_fun_num = pf_fun_num_match.group(1)
    else:
        logger.error("Failed to get function number for %s \n"
                     "and so failed to create a udev rule for renaming "
                     "its' vf-represent" % pf_name)
        return

    udev_data_line = 'SUBSYSTEM=="net", ACTION=="add", ATTR{phys_switch_id}'\
                     '=="%s", ATTR{phys_port_name}=="pf%svf*", '\
                     'IMPORT{program}="%s $attr{phys_port_name}", '\
                     'NAME="%s_$env{NUMBER}"' % (phys_switch_id,
                                                 pf_fun_num,
                                                 _REP_LINK_NAME_FILE,
                                                 pf_name)
    create_rep_link_name_script()
    return add_udev_rule(udev_data_line, _UDEV_RULE_FILE)


def add_udev_rule_to_unmanage_vf_representors_by_nm():
    udev_data_line = 'SUBSYSTEM=="net", ACTION=="add", ATTR{phys_switch_id}'\
                     '!="", ATTR{phys_port_name}=="pf*vf*", '\
                     'ENV{NM_UNMANAGED}="1"'
    return add_udev_rule(udev_data_line, _UDEV_RULE_FILE)


def add_udev_rule(udev_data, udev_file):
    trigger_udev_rule = False
    udev_data = udev_data.strip()
    if not os.path.exists(udev_file):
        with open(udev_file, "w") as f:
            data = "# This file is autogenerated by os-net-config\n%s\n"\
                   % udev_data
            f.write(data)
        reload_udev_rules()
        trigger_udev_rule = True
    else:
        file_data = get_file_data(udev_file)
        udev_lines = file_data.split("\n")
        if udev_data not in udev_lines:
            with open(udev_file, "a") as f:
                f.write(udev_data + "\n")
            reload_udev_rules()
            trigger_udev_rule = True
    return trigger_udev_rule


def reload_udev_rules():
    try:
        processutils.execute('/usr/sbin/udevadm', 'control', '--reload-rules')
        logger.info("udev rules reloaded successfully")
    except processutils.ProcessExecutionError:
        logger.error("Failed to reload udev rules")
        raise


def trigger_udev_rules():
    try:
        processutils.execute('/usr/sbin/udevadm', 'trigger', '--action=add',
                             '--attr-match=subsystem=net')
        logger.info("udev rules triggered successfully")
    except processutils.ProcessExecutionError:
        logger.error("Failed to trigger udev rules")
        raise


def configure_switchdev(pf_name):
    pf_pci = get_pf_pci(pf_name)
    pf_device_id = get_pf_device_id(pf_name)
    if pf_device_id == "0x1013" or pf_device_id == "0x1015":
        try:
            processutils.execute('/usr/sbin/devlink', 'dev', 'eswitch', 'set',
                                 'pci/%s' % pf_pci, 'inline-mode', 'transport')
        except processutils.ProcessExecutionError:
            logger.error("Failed to set inline-mode to transport")
            raise
    try:
        processutils.execute('/usr/sbin/devlink', 'dev', 'eswitch', 'set',
                             'pci/%s' % pf_pci, 'mode', 'switchdev')
    except processutils.ProcessExecutionError:
        logger.error("Failed to set mode to switchdev")
        raise
    logger.info("Device pci/%s set to switchdev mode." % pf_pci)

    # WA to make sure that the uplink_rep is ready after moving to switchdev,
    # as moving to switchdev will remove the sriov_pf and create uplink
    # representor, so we need to make sure that uplink representor is ready
    # before proceed
    _wait_for_uplink_rep_creation(pf_name)

    try:
        processutils.execute('/usr/sbin/ethtool', '-K', pf_name,
                             'hw-tc-offload', 'on')
        logger.info("Enabled \"hw-tc-offload\" for PF %s." % pf_name)
    except processutils.ProcessExecutionError:
        logger.error("Failed to enable hw-tc-offload")
        raise


def run_ip_config_cmd(*cmd, **kwargs):
    logger.info("Running %s" % ' '.join(cmd))
    try:
        processutils.execute(*cmd, **kwargs)
    except processutils.ProcessExecutionError:
        logger.error("Failed to execute %s" % ' '.join(cmd))
        raise


def _pf_interface_up(pf_device):
    if 'promisc' in pf_device:
        run_ip_config_cmd('ip', 'link', 'set', 'dev', pf_device['name'],
                          'promisc', pf_device['promisc'])
    logger.info("Bringing up PF: %s" % pf_device['name'])
    run_ip_config_cmd('ip', 'link', 'set', 'dev', pf_device['name'], 'up')


def get_vendor_id(ifname):
    try:
        with open(os.path.join(_SYS_CLASS_NET, ifname, "device/vendor"),
                  'r') as f:
            out = f.read().strip()
        return out
    except IOError:
        return


def get_pf_pci(pf_name):
    pf_pci_path = os.path.join(_SYS_CLASS_NET, pf_name, "device/uevent")
    pf_info = get_file_data(pf_pci_path)
    pf_pci = re.search(r'PCI_SLOT_NAME=(.*)', pf_info, re.MULTILINE).group(1)
    return pf_pci


def get_pf_device_id(pf_name):
    pf_device_path = os.path.join(_SYS_CLASS_NET, pf_name, "device/device")
    pf_device_id = get_file_data(pf_device_path).strip()
    return pf_device_id


def get_vf_pcis_list(pf_name):
    vf_pcis_list = []
    listOfPfFiles = os.listdir(os.path.join(_SYS_CLASS_NET, pf_name,
                                            "device"))
    for pf_file in listOfPfFiles:
        if pf_file.startswith("virtfn"):
            vf_info = get_file_data(os.path.join(_SYS_CLASS_NET, pf_name,
                                    "device", pf_file, "uevent"))
            vf_pcis_list.append(re.search(r'PCI_SLOT_NAME=(.*)',
                                          vf_info, re.MULTILINE).group(1))
    return vf_pcis_list


def if_down_interface(device):
    logger.info("Running /sbin/ifdown %s" % device)
    try:
        processutils.execute('/sbin/ifdown', device)
    except processutils.ProcessExecutionError:
        logger.error("Failed to ifdown  %s" % device)
        raise


def if_up_interface(device):
    logger.info("Running /sbin/ifup %s" % device)
    try:
        processutils.execute('/sbin/ifup', device)
    except processutils.ProcessExecutionError:
        logger.error("Failed to ifup  %s" % device)
        raise


def configure_sriov_vf():
    sriov_map = _get_sriov_map()
    for item in sriov_map:
        if item['device_type'] == 'vf':
            pf_name = item['device']['name']
            vfid = item['device']['vfid']
            base_cmd = ('ip', 'link', 'set', 'dev', pf_name, 'vf', str(vfid))
            logger.info("Configuring settings for PF: %s VF :%d VF name : %s"
                        % (pf_name, vfid, item['name']))
            if 'macaddr' in item:
                cmd = base_cmd + ('mac', item['macaddr'])
                run_ip_config_cmd(*cmd)
            if 'vlan_id' in item:
                vlan_cmd = base_cmd + ('vlan', str(item['vlan_id']))
                if 'qos' in item:
                    vlan_cmd = vlan_cmd + ('qos', str(item['qos']))
                run_ip_config_cmd(*vlan_cmd)
            if 'spoofcheck' in item:
                cmd = base_cmd + ('spoofchk', item['spoofcheck'])
                run_ip_config_cmd(*cmd)
            if 'state' in item:
                cmd = base_cmd + ('state', item['state'])
                run_ip_config_cmd(*cmd)
            if 'trust' in item:
                cmd = base_cmd + ('trust', item['trust'])
                run_ip_config_cmd(*cmd)
            if 'promisc' in item:
                run_ip_config_cmd('ip', 'link', 'set', 'dev', item['name'],
                                  'promisc', item['promisc'])


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
        '-n', '--numvfs',
        dest="numvfs",
        action='store',
        help="Provide the numvfs for device in the format <device>:<numvfs>",
        required=False)

    opts = parser.parse_args(argv[1:])

    return opts


def configure_logger(verbose=False, debug=False):
    LOG_FORMAT = '[%(asctime)s] [%(levelname)s] %(message)s'
    DATE_FORMAT = '%Y/%m/%d %I:%M:%S %p'
    log_level = logging.WARN

    if debug:
        log_level = logging.DEBUG
    elif verbose:
        log_level = logging.INFO

    logging.basicConfig(format=LOG_FORMAT, datefmt=DATE_FORMAT,
                        level=log_level)


def main(argv=sys.argv):
    opts = parse_opts(argv)
    configure_logger(opts.verbose, opts.debug)

    if opts.numvfs:
        if re.match("^\w+:\d+$", opts.numvfs):
            device_name, numvfs = opts.numvfs.split(':')
            _write_numvfs(device_name, int(numvfs))
        else:
            logging.error("Invalid arguments for --numvfs %s" % opts.numvfs)
            return 1
    else:
        # Configure the PF's
        configure_sriov_pf()
        # Configure the VFs
        configure_sriov_vf()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
