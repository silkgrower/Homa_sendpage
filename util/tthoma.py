#!/usr/bin/python3

# Copyright (c)2023 Homa Developers
# SPDX-License-Identifier: BSD-1-Clause

"""
This script analyzes time traces gathered from Homa in a variety of ways.
Invoke with the --help option for documentation.
"""

from collections import defaultdict
from functools import cmp_to_key
from glob import glob
from optparse import OptionParser
import math
from operator import itemgetter
import os
from pathlib import Path
import re
import string
import sys
import textwrap
import time

# This global variable holds information about every RPC from every trace
# file; it is created by AnalyzeRpcs. Keys are RPC ids, values are dictionaries
# of info about that RPC, with the following elements (some elements may be
# missing if the RPC straddled the beginning or end of the timetrace):
# found:             Time when homa_wait_for_message found the RPC
# gro_core:          Core that handled GRO processing for this RPC
# gro_data:          List of <time, offset, priority> tuples for all incoming
#                    data packets processed by GRO
# gro_data_pkts:     List of packets processed by GRO for this RPC, sorted
#                    in order of 'gro'
# gro_grant:         List of <time, offset> tuples for all incoming
#                    grant packets processed by GRO. Deprecated: use
#                    gro_grant_pkts instead
# gro_grant_pkts:    List of all incoming grant packets processed by GRO
# handoff:           Time when RPC was handed off to waiting thread
# id:                RPC's identifier
# in_length:         Size of the incoming message, in bytes, or None if unknown
# ip_xmits:          Dictionary mapping from offset to ip_*xmit time for
#                    that offset. Only contains entries for offsets where
#                    the ip_xmit record has been seen but not send_data
# node:              'node' field from the trace file where this RPC appeared
#                    (name of trace file without extension)
# out_length:        Size of the outgoing message, in bytes
# peer:              Address of the peer host
# queued:            Time when RPC was added to ready queue (no
#                    waiting threads). At most one of 'handoff' and 'queued'
#                    will be present.
# resend_rx:         List of <time, offset, length> tuples for all incoming
#                    RESEND packets
# resend_tx:         List of <time, offset> tuples for RESEND packets sent
#                    for the incoming message
# retransmits:       One entry for each packet retransmitted; maps from offset
#                    to <time, length> tuple
# softirq_data:      List of <time, offset> tuples for all incoming
#                    data packets processed by SoftIRQ. Deprecated: used
#                    softirq_data_pkts instead
# softirq_data_pkts: List of all incoming data packets processed by SoftIRQ,
#                    sorted in order of 'softirq'
# softirq_grant:     List of <time, offset> tuples for all incoming
#                    grant packets processed by SoftIRQ. Deprecated: use
#                    softirq_grant_pkts instead
# softirq_grant_pkts:List of all incoming grant packets processed by SoftIRQ
# recvmsg_done:      Time when homa_recvmsg returned
# rx_live:           Range of times [start, end] when the incoming message
#                    was in the process of being received. Starts when first
#                    packet is passed to ip*xmit, ends when home_recvmsg
#                    returns. Missing if rx not live during trace.
# sendmsg:           Time when homa_sendmsg was invoked
# send_data:         List of <time, offset, length> tuples for outgoing
#                    data packets (length is message data); time is when
#                    packet was passed to ip*xmit. Deprecated: used
#                    send_data_pkts instead.
# send_data_pkts:    List of outgoing data packets, sorted in order of
#                    'xmit'.
# send_grant:        List of <time, offset, priority> tuples for outgoing
#                    grant packets. Deprecated: used send_grant_pkts instead
# send_grant_pkts:   List of all outgoing grant packets
# tx_live:           Range of times [start, end] when the outgoing message was
#                    partially transmitted. Starts when homa_sendmsg is called,
#                    ends when last data packet is transmitted by the NIC.
#                    Missing if tx isn't live during the trace.
# unsched:           # of bytes of unscheduled data in the incoming message
# free               Time when the RPC was passed to homa_rpc_free
#
# The following fields will be present if homa_rpc_log_active_tt was invoked
# when the timetraces were frozen; they reflect the RPC's state at the end
# of the trace.
# remaining:         # of bytes in the incoming message still to be received
# granted:           # of bytes granted for the incoming message
# sent:              # of bytes that have been sent for the outgoing message
#                    as of the end of the trace
class RpcDict(dict):
    def __missing__(self, id):
        new_rpc = {'node': Dispatcher.cur_trace['node'],
            'gro_data': [],
            'gro_data_pkts': [],
            'gro_grant': [],
            'gro_grant_pkts': [],
            'id': id,
            'in_length': None,
            'softirq_data': [],
            'softirq_data_pkts': [],
            'softirq_grant': [],
            'softirq_grant_pkts': [],
            'send_data': [],
            'send_data_pkts': [],
            'send_grant': [],
            'send_grant_pkts': [],
            'ip_xmits': {},
            'resend_rx': [],
            'resend_tx': [],
            'retransmits': {}
        }
        self[id] = new_rpc
        return new_rpc
rpcs = RpcDict()

# Largest amount of unscheduled data seen any message; set by AnalyzeRpcs.
max_unsched = 0

# This global variable holds information about all of the traces that
# have been read. Maps from the 'node' fields of a trace to a dictionary
# containing the following values:
# file:         Name of file from which the trace was read
# line:         The most recent line read from the file
# node:         The last element of file, with extension removed; used
#               as a host name in various output
# first_time:   Time of the first event read for this trace
# last_time:    Time of the last event read for this trace
# elapsed_time: Total time interval covered by the trace
traces = {}

# Peer address -> node names. Computed by AnalyzeRpcs.
peer_nodes = {}

# This variable holds information about every data packet in the traces.
# it is created by AnalyzePackets. Packets sent with TSO can turn into
# multiple entries in this dictionary, one for each received packet. Keys
# have the form id:offset where id is the RPC id on the sending side and
# offset is the offset in message of the first byte of the packet. Each
# value is a dictionary containing the following fields (some may not
# be present, depending on which events were present in the traces):
# xmit:         Time when ip*xmit was invoked
# nic:          Time when the NIC transmitted the packet (if available)
# gro:          Time when GRO received the packet
# softirq:      Time when homa_softirq processed the packet
# copied:       Time when packet data was copied to user space on receiver
# free:         Time when skb was freed on receiver
# id:           RPC id on the sender
# offset:       Offset of the data in the packet within its message
# length:       # bytes of message data in the received packet
# tso_length:   # bytes of message data in the sent packet. For TSO packets,
#               which are divided into multiple segments, only the first
#               segment will have this field, and it will give the TSO length
#               (before segmentation). This may be an overestimate for the
#               last packet of a message if the message size isn't known.
# segments:     This field will be present in the first packet of each
#               TSO packet (the one with tso_length set); it will be a
#               list of all the other segments deriving from the same
#               TSO packet.
# msg_length:   Total number of bytes in the message, or None if unknown
# priority:     Priority at which packet was transmitted
# tx_node:      Name of node from which the packet was transmitted (always
#               present if xmit is present)
# tx_core:      Core on which ip*xmit was invoked
# tx_qid:       NIC channel on which packet was transmitted
# rx_node:      Name of node on which packet was received
# gro_core:     Core on which homa_gro_receive was invoked
# softirq_core: Core on with SoftIRQ processed the packet
# free_tx_skb:  Time when NAPI released the skb on the sender, which can't
#               happen until the packet has been fully transmitted.
# retransmits:  If the packet was retransmitted, this will be a list of all
#               the times when the packet was retransmiteed.
class PacketDict(dict):
    def __missing__(self, key):
        id_str, offset_str = key.split(':')
        self[key] = {'id': int(id_str), 'offset': int(offset_str)}
        return self[key]
packets = PacketDict()

# offset -> True for each offset that has occurred in a received data packet;
# filled in by AnalyzePackets and AnalyzeRpcs.
recv_offsets = {}

# This variable holds information about every grant packet in the traces.
# it is created by AnalyzePackets. Keys have the form id:offset where id is
# the RPC id on the sending side and offset is the offset in message of
# the first byte of the packet. Each value is a dictionary containing
# the following fields:
# xmit:         Time when ip*xmit was invoked
# nic:          Time when the NIC transmitted the packet
# gro:          Time when GRO received (the first bytes of) the packet
# gro_core:     Core on which homa_gro_receive was invoked
# softirq:      Time when homa_softirq processed the packet
# softirq_core: Core on which SoftIRQ processed the packet
# tx_node:      Node that sent grant (if known)
# id:           Id of the RPC on the sender
# offset:       Offset specified in the grant
# increment:    How much previously ungranted data is covered by this grant;
#               0 if the traces don't contain info about the previous grant
class GrantDict(dict):
    def __missing__(self, key):
        id_str, offset_str = key.split(':')
        self[key] = {'id': int(id_str), 'offset': int(offset_str),
                'increment': 0}
        return self[key]
grants = GrantDict()

# Node -> list of intervals for that node. Created by the intervals analyzer.
# Each interval contains information about a particular time range, including
# things that happened during that time range and the state of the node at
# the end of the period. The list entry for each interval is a dictionary with
# the following fields:repo_
# time:           Ending time of the interval (integer usecs); this time is
#                 included in the interval
# rpcs_live:      Number of live RPCs for which this node is the client
# tx_live_req:    Number of outgoing request messages with unsent data as
#                 of the end of the interval
# tx_live_resp:   Number of outgoing response messages with unsent data as
#                 of the end of the interval
# tx_starts:      Number of new outgoing messages that started in the interval
# tx_pkts:        Number of data packets passed to ip*xmit during the interval
# tx_bytes:       Number of bytes of data passed to ip*xmit during the interval
# tx_nic_pkts:    Number of data packets passed to the NIC during the interval
# tx_nic_bytes:   Number of bytes of data passed to the NIC during the interval
# tx_in_nic:      Number of bytes of data that have been passed to the NIC
#                 but not yet returned via the tx completion queue, as of the
#                 end of the interval
# tx_qdisc:       Bytes of data that have been passed to ip*xmit but not
#                 yet transmitted, as of the end of the interval (large
#                 numbers probably due to qdisc)
# tx_q:           Estimate of the number of unsent bytes in the NIC (based
#                 on when packets passed to the NIC if available, otherwise
#                 when passed to ip*xmit)
# tx_free_bytes:  Bytes of data freed after NIC notified tx completion
# tx_max_free:    Largest value of pkt['free_tx_skb'] - pkt['nic'] for
#                 a packet freed in this interval (0 if no packets freed)
# tx_min_free:    Smallest value of pkt['free_tx_skb'] - pkt['nic'] for
#                 a packet freed in this interval (0 if no packets freed)
# tx_max_gro_free:Largest value of pkt['gro'] - pkt['free_tx_skb'] for
#                 any segment of a packet freed in this interval (None if
#                 no packets freed)
# tx_min_gro_free:Smallest value of pkt['gro'] - pkt['free_tx_skb'] for
#                 any segment of a packet freed in this interval (None if
#                 no packets freed)
# tx_grant_xmit:  Bytes of grant that have been passsed to ip*xmit but not yet
#                 received by GRO, as of the end of the interval
# tx_grant_gro:   Bytes of grant that have been received by GRO but not SoftIRQ,
#                 as of the end of the interval
# tx_grant_avl:   Bytes of grant that have been received by SoftIRQ but not
#                 yet transmitted, as of the end of the interval
# tx_new_grants:  Bytes of grants that became available at SoftIRQ level
#                 during the interval
#
# rx_starts:      Number of new incoming messages whose first packet was
#                 transmitted by the sender in the interval
# rx_live:        Number of incoming messages that have been partially
#                 transmitted and/or received as of the end of the interval
# rx_pkts:        Number of data packets received by GRO during the interval
# rx_bytes:       Number of bytes of data received by GRO during the interval
# rx_grantable:   Number of incoming RPCs that have not been fully granted
# rx_granted:     Bytes of grant that have been transmitted, but for which
#                 corresponding data has not been transmitted
# rx_data_xmit:   Number of bytes of data that have been passed to ip*xmit by
#                 the sender but not yet received by GRO, as of the end of
#                 the interval
# rx_data_qdisc:  Number of bytes of data that have been passed to ip*xmit
#                 by the sender but haven't actually been handed off to
#                 the NIC (they are queued in qdisc).
# rx_overdue:     Number of bytes of data that were transmitted by the NIC
#                 at least 100 us ago but haven't yet been seen by GRO
# rx_data_gro:    Number of bytes of data that have been received by GRO
#                 but not SoftIRQ, as of the end of the interval
# rx_new_grants:  Number of bytes of additional grants passed to ip*xmit
#                 during the interval
#
# The following fields are present only if the grants analyzer is used:
# rx_grants:      Number of incoming RPCs with outstanding grants
# rx_grant_bytes: Total bytes of data in outstanding grants for incoming RPCs
# rx_grant_info:  Formatted text describing incoming RPCs with oustanding grants
#                 as of the end of the interval
# tx_grant_info:  Formatted text describing outgoing RPCs with available grants
#                 as of the end of the interval
intervals = None

def add_to_intervals(node, start, end, key, delta):
    """
    Find all of the intervals for node whose end times overlap the range
    (start, end], then add delta to the key entry for each of those intervals.
    """

    global intervals, options
    node_intervals = intervals[node]
    t0 = node_intervals[0]['time'] - options.interval
    if start < t0:
        first = 0
    else:
        first = int((start - t0)/options.interval)
    for interval in node_intervals[first:]:
        if interval['time'] > end:
            break
        interval[key] += delta

def bytes_to_usec(bytes):
    """
    Compute how long many microseconds it takes to transmit a given number of
    bytes, assuming a network speed equal to the --gbps option.
    """
    global options

    return (bytes*8) / (options.gbps * 1000)

def cmp_pkts(p1, p2, field):
    """
    Compute a sorting order among packets to print, using a given field
    of the packets (which contains a time value), which may not be present.
    """

    if field in p1:
        if field in p2:
            return p1[field] - p2[field]
        else:
            return 1
    elif field in p2:
        return -1
    if p1['id'] != p2['id']:
        return p1['id'] - p2['id']
    return p1['offset'] - p2['offset']

def dict_avg(data, key):
    """
    Given a list of dictionaries, return the average of the elements
    with the given key.
    """
    count = 0
    total = 0.0
    for item in data:
        if (key in item) and (item[key] != None):
            total += item[key]
            count += 1
    if not count:
        return 0
    return total / count

def list_avg(data, index):
    """
    Given a list of lists, return the average of the index'th elements
    of the lists.
    """
    if len(data) == 0:
        return 0
    total = 0
    for item in data:
        total += item[0]
    return total / len(data)

def extract_num(s):
    """
    If the argument contains an integer number as a substring,
    return the number. Otherwise, return None.
    """
    match = re.match('[^0-9]*([0-9]+)', s)
    if match:
        return int(match.group(1))
    return None

def gbps(bytes, usecs):
    """
    Compute the data rate in Gbps for data transmitted or received in
    an interval.

    bytes:   Number of bytes transferred
    usecs:   Amount of time (microseconds) during which the transfer happened
    """
    global options

    return ((bytes*8)/usecs)*1e-3

def get_first_interval_end(node=None):
    """
    Used when writing out data at regular intervals during the traces.
    Returns the end time of the first interval that contains any trace data.

    node:   Name of a node: if specified, returns the first interval that
            contains data for this node; otherwise returns the first interval
            that contains data for any node
    """
    global traces, options

    if node == None:
        start = get_first_time()
    else:
        start = traces[node]['first_time']
    interval_end = int(start)//options.interval * options.interval
    if interval_end < start:
        interval_end += options.interval
    return interval_end

def get_first_time():
    """
    Return the earliest event time across all trace files.
    """
    earliest = 1e20
    for trace in traces.values():
        first = trace['first_time']
        if first < earliest:
            earliest = first
    return earliest

def get_granted(rpc, time):
    """
    Returns the offset of the last grant sent for an RPC as of a given time,
    or None if no data available.
    """
    max_offset = -1
    for pkt in rpc['send_grant_pkts']:
        t = pkt['xmit']
        offset = pkt['offset']
        if (t < time) and (offset > max_offset):
            max_offset = offset
    if max_offset >= 0:
        return max_offset
    return None

def get_interval(node, usecs):
    """
    Returns the interval dictionary corresponding to the arguments. A
    new interval is created if the desired interval doesn't exist. Returns None
    if the interval ends before the first trace record for the node or starts
    after the last record for the node, or if there is no interval
    information associated with node. The intervals structure must have been
    initialized before this function is called.

    node:     Name of the desired node
    usecs:    Time whose interval is desired. Intervals include their
              ending time but not their starting time
    """
    global intervals, options, traces

    if not node in intervals:
        return None
    data = intervals[node]
    interval_length = options.interval
    first_end = data[0]['time']
    # Adjust time down slightly to ensure that even intervals (e.g. 500.0)
    # end up in the right place.
    i = int((usecs - 1e-5 - (first_end - interval_length)) / interval_length)
    if i < 0 or i >= len(data):
        return None
    return data[i]

def get_last_time():
    """
    Return the latest event time across all trace files.
    """
    latest = -1e20
    for trace in traces.values():
        last = trace['last_time']
        if last > latest:
            latest = last
    return latest

def get_max_gro(pkt):
    """
    If pkt is a TSO packet that was divided into multiple segments, returns
    the largest 'gro' from any segment. Otherwise returns pkt['gro'] if it
    exists (result could be None).
    """
    max_gro = 1e-20
    if 'gro' in pkt:
        max_gro = pkt['gro']
    if 'segments' in pkt:
        for seg in pkt['segments']:
            if 'gro' in seg:
                max_gro = max(max_gro, seg['gro'])
    if max_gro == 1e-20:
        return None
    return max_gro

def get_mtu():
    """
    Returns the amount of message data in a full-size network packet (as
    received by the receiver; GSO packets sent by senders may be larger).
    """

    # Use get_recv_length to do all of the work.
    get_recv_length(0)
    return get_recv_length.mtu

def get_packet(id, offset):
    """
    Returns the entry in packets corresponding to id and offset.
    """
    global packets
    return packets['%d:%d' % (id, offset)]

def get_recv_length(offset, msg_length=None):
    """
    Compute the length of a received packet. Uses information collected in the
    recv_offsets global variable, and assumes that all messages use the same
    set of offsets.

    offset:      Offset of the first byte in the packet.
    msg_length:  Total number of bytes in the message, if known. If not
                 supplied, then the last packet in a message may have its
                 length overestimated.
    """
    global recv_offsets
    if len(get_recv_length.lengths) != len(recv_offsets):
        # Must recompute lengths (new offsets have appeared)
        get_recv_length.lengths = {}
        sorted_offsets = sorted(recv_offsets.keys())
        max = 0
        for i in range(len(sorted_offsets)-1):
            length = sorted_offsets[i+1] - sorted_offsets[i]
            if length > max:
                max = length;
            get_recv_length.lengths[sorted_offsets[i]] = length
        get_recv_length.lengths[sorted_offsets[-1]] = max
        get_recv_length.mtu = max
    if offset in get_recv_length.lengths:
        length = get_recv_length.lengths[offset]
    else:
        length = get_recv_length.mtu
    if (msg_length != None) and ((offset + length) > msg_length):
        length = msg_length - offset
    return length

def get_received(rpc, time):
    """
    Returns the offset of the byte just after the last one received by
    SoftIRQ for an RPC as of a given time, or None if no data available.
    Assumes that the rpc analyzer has run.
    """
    max_recv = -1
    for pkt in rpc['softirq_data_pkts']:
        t = pkt['softirq']
        recv = pkt['offset'] + pkt['length']
        if (t < time) and (recv > max_recv):
            max_recv = recv
    if max_recv >= 0:
        return max_recv

    # No packets have been received by SoftIRQ
    if ('recvmsg_done' in rpc) and (rpc['recvmsg_done'] < time):
        return rpc['in_length']
    # If there are GRO packets, assume last SoftIRQ packet is the one
    # just before the first GRO packet.
    min_offset = 1e20
    for pkt in rpc['gro_data_pkts']:
        offset = pkt['offset']
        if offset < min_offset:
            min_offset = offset
    if (min_offset < 1e20) and (min_offset > 0):
        return min_offset
    return None

# offset -> max packet length for that offset.
get_recv_length.lengths = {}
# Maximum length for any offset.
get_recv_length.mtu = 0

def get_rpc_node(id):
    """
    Given an RPC id, return the name of the node corresponding
    to that id, or None if a node could not be determined.
    """
    global rpcs, traces
    if id in rpcs:
        return rpcs[id]['node']
    if id^1 in rpcs:
        rpc = rpcs[id^1]
        if 'peer' in rpc:
            return peer_nodes[rpc['peer']]
    return None

def get_sorted_nodes():
    """
    Returns a list of node names ('node' value from traces), sorted
    by node number if there are numbers in the names, otherwise
    sorted alphabetically.
    """
    global traces

    # We cache the result to avoid recomputing
    if get_sorted_nodes.result != None:
        return get_sorted_nodes.result

    # First see if all of the names contain numbers.
    nodes = traces.keys()
    got_nums = True
    for node in nodes:
        if extract_num(node) == None:
            got_nums = False
            break
    if not got_nums:
        get_sorted_nodes.result = sorted(nodes)
    else:
        get_sorted_nodes.result = sorted(nodes, key=lambda name : extract_num(name))
    return get_sorted_nodes.result
get_sorted_nodes.result = None

def get_time_stats(samples):
    """
    Given a list of elapsed times, returns a string containing statistics
    such as min time, P99, and average.
    """
    if not samples:
        return 'no data'
    sorted_data = sorted(samples)
    average = sum(sorted_data)/len(samples)
    return 'Min %.1f, P50 %.1f, P90 %.1f, P99 %.1f, Avg %.1f' % (
            sorted_data[0],
            sorted_data[50*len(sorted_data)//100],
            sorted_data[90*len(sorted_data)//100],
            sorted_data[99*len(sorted_data)//100],
            average)

def get_xmit_time(offset, rpc, rx_time=1e20):
    """
    Returns the time when a given offset was transmitted by an RPC. If
    there is not a precise record of this, estimate the time based on other
    packets sent for the RPC. If we couldn't even make a reasonable estimate,
    then None is returned.

    offset:   Offset within the outgoing message for rpc
    rpc:      An entry in the global variable "rpcs"
    rx_time:  Time when the packet was received; omit if unknown
    """

    xmit = rx_time
    fallback = None
    for pkt_time, pkt_offset, length in rpc['send_data']:
        if offset < pkt_offset:
            if (fallback == None) and (pkt_time < rx_time):
                # No record so far for the desired packet; use the time from
                # the next packet as a fall-back.
                fallback = pkt_time
        elif offset < (pkt_offset + length):
            if (pkt_time < rx_time):
                xmit = pkt_time
        if pkt_time >= rx_time:
            break
    if xmit == 1e20:
        return fallback
    return xmit

def percentile(data, pct, format, na):
    """
    Finds the element of data corresponding to a given percentile pct
    (0 is first, 100 or more is last), formats it according to format,
    and returns the result. Returns na if the list is empty. Data must
    be sorted in percentile order
    """
    if len(data) == 0:
        return na
    i = int(pct*len(data)/100)
    if i >= len(data):
        i = len(data) - 1
    return format % (data[i])

def pkt_id(id, offset):
    return '%d:%d' % (id, offset)

def print_analyzer_help():
    """
    Prints out documentation for all of the analyzers.
    """

    global options
    analyzers = options.analyzers.split()
    module = sys.modules[__name__]
    for attr in sorted(dir(module)):
        if not attr.startswith('Analyze'):
            continue
        object = getattr(module, attr)
        analyzer = attr[7].lower() + attr[8:]
        if (options.analyzers != 'all') and (not analyzer in analyzers):
            continue
        if hasattr(object, 'output'):
            print('%s: %s' % (analyzer, object.__doc__))

def print_field_if(dict, field, fmt, modifier=None):
    """
    Format a given field in a dictionary, if it is present. If the field
    isn't present, return an empty string.
    dict:      Dictionary containing the desired field.
    field:     Name of field within dictionary.
    fmt:       Format string (e.g. %7.1f) to apply to the field, if it is
               present.
    modifier:  If specified, this is a lambda that is applied to the field
               to modify its value before formatting.
    """
    if field in dict:
        value = dict[field]
        if modifier != None:
            value = modifier(value)
        return fmt % (value)
    return ''

def print_if(value, fmt, modifier=None):
    """
    Format a value if it isn't None, otherwise return an empty string.
    value:     Value to format.
    fmt:       Format string (e.g. %7.1f) to apply to the value, if it is
               not None.
    modifier:  If specified, this is a lambda that is applied to the field
               to modify its value before formatting.
    """
    if value != None:
        if modifier != None:
            value = modifier(value)
        return fmt % (value)
    return ''

def require_options(analyzer, *args):
    """
    For each argument, ensures that the associated option has been specified;
    raises an exception if it hasn't. The analyzer argument gives the name
    of the analyzer requiring the options, for use in the exception message.
    """
    global options
    for arg in args:
        if getattr(options, arg) == None:
            raise Exception('The %s analyzer requires the --%s option' % (
                    analyzer, arg))

def sum_fields(list, field):
    """
    Given a list of dictionaries, return the sum of a given field in each
    of the dictionaries.
    """
    total = 0
    for d in list:
        total += d[field]
    return total

class Dispatcher:

    # Info about trace file currently being parsed, or None if none.
    cur_trace = None

    """
    This class manages a set of patterns to match against the records
    of a timetrace. It then reads  time trace files and passes information
    about matching records to other classes that are interested in them.
    """

    def __init__(self):
        # List of all objects with registered interests, in order of
        # registration.
        self.objs = []

        # Keys are names of all classes passed to the interest method.
        # Values are the corresponding objects.
        self.analyzers = {}

        # Pattern name -> list of objects interested in that pattern.
        self.interests = {}

        # List of objects with tt_all methods, which will be invoked for
        # every record.
        self.all_interests= []

        # Pattern prefix -> list of patterns with that prefix. All of the
        # keys have the same length, given by self.prefix_length. Entries
        # in each list have the same order that they appear in patterns.
        # Setting this to None causes it to be recomputed the next time
        # a trace file is read
        self.parse_table = None

        # The number of initial characters of the message portion of a
        # trace record that are used to lookup in parse_table. This is
        # the largest number such that each pattern has at least this many
        # literal initial characters.
        self.prefix_length = -1

        # Total nanoseconds spent parsing trace files so far.
        self.parse_ns = 0

        # Total number of lines parsed from trace files so far.
        self.trace_lines = 0

        # Total number of times regexps were applied to lines of trace
        # files (whether they matched or not)
        self.regex_tries = 0

        for pattern in self.patterns:
            pattern['matches'] = 0

    def get_analyzer(self, name):
        """
        Return the analyzer object associated with name, or None if
        there is no such analyzer.

        name:   Name of an analyzer class.
        """

        if name in self.analyzers:
            return self.analyzers[name]
        else:
            return None

    def get_analyzers(self):
        """
        Return a list of all analyzer objects registered with this
        dispatcher
        """

        return self.objs

    def interest(self, analyzer):
        """
        If analyzer hasn't already been registered with this dispatcher,
        create an instance of that class and arrange for its methods to
        be invoked for matching lines in timetrace files. For each method
        named 'tt_xxx' in the class there must be a pattern named 'xxx';
        the method will be invoked whenever the pattern matches a timetrace
        line, with parameters containing parsed fields from the line. In any
        case, returns analyzer object.

        analyzer: name of a class containing trace analysis code
        """

        if analyzer in self.analyzers:
            return self.analyzers[analyzer]

        # This line breaks circular dependency chains.
        self.analyzers[analyzer] = None
        obj = getattr(sys.modules[__name__], analyzer)(self)
        self.analyzers[analyzer] = obj
        self.objs.append(obj)

        for name in dir(obj):
            if not name.startswith('tt_'):
                continue
            method = getattr(obj, name)
            if not callable(method):
                continue
            name = name[3:]
            if name == 'all':
                self.all_interests.append(obj)
                continue
            for pattern in self.patterns:
                if name != pattern['name']:
                    continue
                found_pattern = True
                if not name in self.interests:
                    self.interests[name] = []
                self.interests[name].append(obj)
                break
            if not name in self.interests:
                raise Exception('Couldn\'t find pattern %s for analyzer %s'
                        % (name, analyzer))
        return obj

    def parse(self, file):
        """
        Parse a timetrace file and invoke interests.
        file:     Name of the file to parse.
        """

        global traces
        start_ns = time.time_ns()
        self.__build_parse_table()
        prefix_matcher = re.compile(' *([-0-9.]+) us .* \[C([0-9]+)\] (.*)')

        trace = {}
        trace['file'] = file
        node = Path(file).stem
        trace['node'] = node
        traces[node] = trace
        Dispatcher.cur_trace = trace

        print('Reading trace file %s' % (file), file=sys.stderr)
        for analyzer in self.objs:
            if hasattr(analyzer, 'init_trace'):
                analyzer.init_trace(trace)

        f = open(file)
        first = True
        for trace['line'] in f:
            # Parse each line in 2 phases: first the time and core information
            # that is common to all patterns, then the message, which will
            # select at most one pattern.
            self.trace_lines += 1
            self.regex_tries += 1
            match = prefix_matcher.match(trace['line'])
            if not match:
                continue
            t = float(match.group(1))
            core = int(match.group(2))
            msg = match.group(3)

            if first:
                trace['first_time'] = t
                first = False
            trace['last_time'] = t
            prefix = msg[0:self.prefix_length]
            if prefix in self.parse_table:
                for pattern in self.parse_table[prefix]:
                    self.regex_tries += 1
                    match = pattern['cregexp'].match(msg)
                    if match:
                        pattern['matches'] += 1
                        pattern['parser'](trace, t, core, match,
                                self.interests[pattern['name']])
                        break
            for interest in self.all_interests:
                interest.tt_all(trace, t, core, msg)
        f.close()
        trace['elapsed_time'] = trace['last_time'] - trace['first_time']
        self.parse_ns += time.time_ns() - start_ns;
        Dispatcher.cur_trace = None

    def print_no_matches(self):
        """
        Print out information about patterns that didn't match any lines
        in any trace file.
        """
        no_matches = []
        for patterns in self.parse_table.values():
            for pattern in patterns:
                if pattern['matches'] > 0:
                    continue
                no_matches.append(pattern)
        if no_matches:
            print('No lines matched the following patterns:', file=sys.stderr)
            for pattern in no_matches:
                print_string = pattern['regexp']
                match = re.search('[()[\].+*?\\^${}]', print_string)
                if match:
                    print_string = print_string[:match.start()]
                print('  %s...' % (print_string), file=sys.stderr)

    def print_stats(self):
        """
        Print statistics about the efficiency of parsing trace files.
        """
        print('Trace file lines read: %d' % (self.trace_lines))
        print('Regex matches attempted: %d (%.1f per line)' % (
                self.regex_tries, self.regex_tries/self.trace_lines))
        print('Trace file parse time: %.3f sec' % (self.parse_ns*1e-9))
        print('(%.1f usec/line, %.1f usec/regex attempt)' % (
                ((self.parse_ns/self.trace_lines)*1e-3),
                ((self.parse_ns/self.regex_tries)*1e-3)))

    def __build_parse_table(self):
        """
        Builds self.parse_table. Also sets the 'parser' and 'cregexp' elements
        for each pattern.
        """
        if self.parse_table != None:
            return
        self.parse_table = defaultdict(list)

        # Pass 1: first compute self.prefix_length and set the 'parser'
        # and 'cregexp' elements of pattern entries.
        self.prefix_length = 1000
        for pattern in self.patterns:
            meta_matcher = re.compile('[()[\].+*?\\^${}]')
            pattern['parser'] = getattr(self, '_Dispatcher__' + pattern['name'])
            pattern['cregexp'] = re.compile(pattern['regexp'])
            if pattern['name'] in self.interests:
                match = meta_matcher.search(pattern['regexp'])
                if not match:
                    length = len(pattern['regexp'])
                else:
                    length = match.start()
                if length < self.prefix_length:
                    self.prefix_length = length;

        # Pass 2: fill in self.parse_table
        for pattern in self.patterns:
            if pattern['name'] in self.interests:
                prefix = pattern['regexp'][0:self.prefix_length]
                self.parse_table[prefix].append(pattern)

    # Each entry in this list represents one pattern that can be matched
    # against the lines of timetrace files. Each pattern is a dictionary
    # containing the following elements:
    # name:       Name for this pattern. Used for auto-configuration (e.g.
    #             methods named tt_<name> are invoked to handle matching
    #             lines).
    # regexp:     Regular expression to match against the message portion
    #             of timetrace records (everything after the core number).
    #             For efficient matching, there should be several literal
    #             characters before any regexp metachars.
    # cregexp:    Compiled version of regexp.
    # matches:    Number of timetrace lines that matched this pattern.
    # parser:     Method in this class that will be invoked to do additional
    #             parsing of matched lines and invoke interests.
    # This object is initialized as the parser methods are defined below.
    patterns = []

    # The declarations below define parser methods and their associated
    # patterns. The name of a parser is derived from the name of its
    # pattern. Parser methods are invoked when lines match the corresponding
    # pattern. The job of each method is to parse the matches from the pattern,
    # if any, and invoke all of the relevant interests. All of the methods
    # have the same parameters:
    # self:         The Dispatcher object
    # trace:        Holds information being collected from the current trace file
    # time:         Time of the current record (microseconds)
    # core:         Number of the core on which the event occurred
    # match:        The match object returned by re.match
    # interests:    The list of objects to notify for this event

    def __gro_data(self, trace, time, core, match, interests):
        peer = match.group(1)
        id = int(match.group(2))
        offset = int(match.group(3))
        prio = int(match.group(4))
        for interest in interests:
            interest.tt_gro_data(trace, time, core, peer, id, offset, prio)

    patterns.append({
        'name': 'gro_data',
        'regexp': 'homa_gro_receive got packet from ([^ ]+) id ([0-9]+), '
                  'offset ([0-9.]+), priority ([0-9.]+)'
    })

    def __gro_grant(self, trace, time, core, match, interests):
        peer = match.group(1)
        id = int(match.group(2))
        offset = int(match.group(3))
        priority = int(match.group(4))
        for interest in interests:
            interest.tt_gro_grant(trace, time, core, peer, id, offset, priority)

    patterns.append({
        'name': 'gro_grant',
        'regexp': 'homa_gro_receive got grant from ([^ ]+) id ([0-9]+), '
                  'offset ([0-9]+), priority ([0-9]+)'
    })

    def __softirq_data(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        msg_length = int(match.group(3))
        for interest in interests:
            interest.tt_softirq_data(trace, time, core, id, offset, msg_length)

    patterns.append({
        'name': 'softirq_data',
        'regexp': 'incoming data packet, id ([0-9]+), .*, offset ([0-9.]+)'
                  '/([0-9.]+)'
    })

    def __softirq_grant(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        priority = int(match.group(3))
        increment = int(match.group(4))
        for interest in interests:
            interest.tt_softirq_grant(trace, time, core, id, offset, priority,
                    increment)

    patterns.append({
        'name': 'softirq_grant',
        'regexp': 'processing grant for id ([0-9]+), offset ([0-9]+), '
                'priority ([0-9]+), increment ([0-9]+)'
    })

    def __ip_xmit(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        for interest in interests:
            interest.tt_ip_xmit(trace, time, core, id, offset)

    patterns.append({
        'name': 'ip_xmit',
        'regexp': 'calling ip.*_xmit: .* id ([0-9]+), offset ([0-9]+)'
    })

    def __send_data(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        length = int(match.group(3))
        if length == 0:
            # Temporary fix to compensate for Homa bug; delete this code soon.
            return
        for interest in interests:
            interest.tt_send_data(trace, time, core, id, offset, length)

    patterns.append({
        'name': 'send_data',
        'regexp': 'Finished queueing packet: rpc id ([0-9]+), offset '
                  '([0-9]+), len ([0-9]+)'
    })

    def __send_grant(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        priority = int(match.group(3))
        increment = int(match.group(4))
        for interest in interests:
            interest.tt_send_grant(trace, time, core, id, offset, priority,
                    increment)

    patterns.append({
        'name': 'send_grant',
        'regexp': 'sending grant for id ([0-9]+), offset ([0-9]+), '
                  'priority ([0-9]+), increment ([0-9]+)'
    })

    def __qdisc_queue_data(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        qid = int(match.group(3))
        queue = match.group(4)
        for interest in interests:
            interest.tt_qdisc_queue_data(trace, time, core, id,
                    offset, qid, queue)

    patterns.append({
        'name': 'qdisc_queue_data',
        'regexp': '__dev_xmit_skb queueing homa data packet for '
                  'id ([0-9]+), offset ([0-9]+), qid ([0-9]+) \(([^)]+)\)'
    })

    def __mlx_data(self, trace, time, core, match, interests):
        peer = match.group(1)
        id = int(match.group(2))
        offset = int(match.group(3))
        for interest in interests:
            interest.tt_mlx_data(trace, time, core, peer, id, offset)

    patterns.append({
        'name': 'mlx_data',
        'regexp': 'mlx sent homa data packet to ([^,]+), id ([0-9]+), '
                  'offset ([0-9]+)'
    })

    def __mlx_grant(self, trace, time, core, match, interests):
        peer = match.group(1)
        id = int(match.group(2))
        offset = int(match.group(3))
        for interest in interests:
            interest.tt_mlx_grant(trace, time, core, peer, id, offset)

    patterns.append({
        'name': 'mlx_grant',
        'regexp': 'mlx sent homa grant to ([^,]+), id ([0-9]+), offset ([0-9]+)'
    })

    def __free_tx_skb(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        qid = int(match.group(3))
        msg_length = int(match.group(4))
        for interest in interests:
            interest.tt_free_tx_skb(trace, time, core, id, offset, qid,
                    msg_length)

    patterns.append({
        'name': 'free_tx_skb',
        'regexp': 'napi freeing tx skb for homa data, id ([0-9]+), '
                'offset ([0-9]+), qid ([0-9]+), msg_length ([0-9]+)'
    })

    def __sendmsg_request(self, trace, time, core, match, interests):
        peer = match.group(1)
        id = int(match.group(2))
        length = int(match.group(3))
        for interest in interests:
            interest.tt_sendmsg_request(trace, time, core, peer, id, length)

    patterns.append({
        'name': 'sendmsg_request',
        'regexp': 'homa_sendmsg request, target ([^: ]+):.* id '
                  '([0-9]+), length ([0-9]+)'
    })

    def __sendmsg_response(self, trace, time, core, match, interests):
        id = int(match.group(1))
        length = int(match.group(2))
        for interest in interests:
            interest.tt_sendmsg_response(trace, time, core, id, length)

    patterns.append({
        'name': 'sendmsg_response',
        'regexp': 'homa_sendmsg response, id ([0-9]+), .*length ([0-9]+)'
    })

    def __sendmsg_done(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_sendmsg_done(trace, time, core, id)

    patterns.append({
        'name': 'sendmsg_done',
        'regexp': 'homa_sendmsg finished, id ([0-9]+)'
    })

    def __recvmsg_done(self, trace, time, core, match, interests):
        id = int(match.group(1))
        length = int(match.group(2))
        for interest in interests:
            interest.tt_recvmsg_done(trace, time, core, id, length)

    patterns.append({
        'name': 'recvmsg_done',
        'regexp': 'homa_recvmsg returning id ([0-9]+), length ([0-9]+)'
    })

    def __copy_in_start(self, trace, time, core, match, interests):
        for interest in interests:
            interest.tt_copy_in_start(trace, time, core)

    patterns.append({
        'name': 'copy_in_start',
        'regexp': 'starting copy from user space'
    })

    def __copy_in_done(self, trace, time, core, match, interests):
        id = int(match.group(1))
        num_bytes = int(match.group(2))
        for interest in interests:
            interest.tt_copy_in_done(trace, time, core, id, num_bytes)

    patterns.append({
        'name': 'copy_in_done',
        'regexp': 'finished copy from user space for id ([-0-9.]+), '
                'length ([-0-9.]+)'
    })

    def __copy_out_start(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_copy_out_start(trace, time, core, id)

    patterns.append({
        'name': 'copy_out_start',
        'regexp': 'starting copy to user space for id ([0-9]+)'
    })

    def __copy_out_done(self, trace, time, core, match, interests):
        start = int(match.group(1))
        end = int(match.group(2))
        id = int(match.group(3))
        for interest in interests:
            interest.tt_copy_out_done(trace, time, core, id, start, end)

    patterns.append({
        'name': 'copy_out_done',
        'regexp': 'copied out bytes ([0-9.]+)-([0-9.]+) for id ([0-9.]+)'
    })

    def __free_skbs(self, trace, time, core, match, interests):
        num_skbs = int(match.group(1))
        for interest in interests:
            interest.tt_free_skbs(trace, time, core, num_skbs)

    patterns.append({
        'name': 'free_skbs',
        'regexp': 'finished freeing ([0-9]+) skbs'
    })

    def __gro_handoff(self, trace, time, core, match, interests):
        softirq_core = int(match.group(1))
        for interest in interests:
            interest.tt_gro_handoff(trace, time, core, softirq_core)

    patterns.append({
        'name': 'gro_handoff',
        'regexp': 'homa_gro_.* chose core ([0-9]+)'
    })

    def __softirq_start(self, trace, time, core, match, interests):
        for interest in interests:
            interest.tt_softirq_start(trace, time, core)

    patterns.append({
        'name': 'softirq_start',
        'regexp': 'homa_softirq: first packet'
    })

    def __rpc_handoff(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_rpc_handoff(trace, time, core, id)

    patterns.append({
        'name': 'rpc_handoff',
        'regexp': 'homa_rpc_handoff handing off id ([0-9]+)'
    })

    def __rpc_queued(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_rpc_queued(trace, time, core, id)

    patterns.append({
        'name': 'rpc_queued',
        'regexp': 'homa_rpc_handoff finished queuing id ([0-9]+)'
    })

    def __wait_found_rpc(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_wait_found_rpc(trace, time, core, id)

    patterns.append({
        'name': 'wait_found_rpc',
        'regexp': 'homa_wait_for_message found rpc id ([0-9]+)'
    })

    def __poll_success(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_poll_success(trace, time, core, id)

    patterns.append({
        'name': 'poll_success',
        'regexp': 'received RPC handoff while polling, id ([0-9]+)'
    })

    def __resend_tx(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        for interest in interests:
            interest.tt_resend_tx(trace, time, core, id, offset)

    patterns.append({
        'name': 'resend_tx',
        'regexp': 'Sent RESEND for client RPC id ([0-9]+), .* offset ([0-9]+)'
    })

    def __resend_rx(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        length = int(match.group(2))
        for interest in interests:
            interest.tt_resend_rx(trace, time, core, id, offset, length)

    patterns.append({
        'name': 'resend_rx',
        'regexp': 'resend request for id ([0-9]+), offset ([0-9]+), '
                'length ([0-9]+)'
    })

    def __retransmit(self, trace, time, core, match, interests):
        offset = int(match.group(1))
        length = int(match.group(2))
        id = int(match.group(3))
        for interest in interests:
            interest.tt_retransmit(trace, time, core, id, offset, length)

    patterns.append({
        'name': 'retransmit',
        'regexp': 'retransmitting offset ([0-9]+), length ([0-9]+), id ([0-9]+)'
    })

    def __unsched(self, trace, time, core, match, interests):
        id = int(match.group(1))
        num_bytes = int(match.group(2))
        for interest in interests:
            interest.tt_unsched(trace, time, core, id, num_bytes)

    patterns.append({
        'name': 'unsched',
        'regexp': 'Incoming message for id ([0-9]+) has ([0-9]+) unscheduled'
    })

    def __lock_wait(self, trace, time, core, match, interests):
        event = match.group(1)
        lock_name = match.group(2)
        for interest in interests:
            interest.tt_lock_wait(trace, time, core, event, lock_name)

    patterns.append({
        'name': 'lock_wait',
        'regexp': '(beginning|ending) wait for (.*) lock'
    })

    def __resend_busy(self, trace, time, core, match, interests):
        id = int(match.group(1))
        state = int(match.group(2))
        for interest in interests:
            interest.tt_resend_busy(trace, time, core, id, state)

    patterns.append({
        'name': 'resend_busy',
        'regexp': 'sending BUSY from resend, id ([0-9]+), state ([0-9]+)'
    })

    def __softirq_resend(self, trace, time, core, match, interests):
        id = int(match.group(1))
        offset = int(match.group(2))
        length = int(match.group(3))
        prio = int(match.group(4))
        for interest in interests:
            interest.tt_softirq_resend(trace, time, core, id, offset,
                    length, prio)

    patterns.append({
        'name': 'softirq_resend',
        'regexp': 'resend request for id ([0-9]+), offset ([0-9]+), '
                'length ([0-9]+), prio ([0-9]+)'
    })

    def __bpages_alloced(self, trace, time, core, match, interests):
        id = int(match.group(1))
        bpages = int(match.group(2))
        for interest in interests:
            interest.tt_bpages_alloced(trace, time, core, id, bpages)

    patterns.append({
        'name': 'bpages_alloced',
        'regexp': 'RPC id ([0-9]+) has ([0-9]+) bpages allocated'
    })

    def __rpc_free(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_rpc_free(trace, time, core, id)

    patterns.append({
        'name': 'rpc_free',
        'regexp': 'homa_rpc_free invoked for id ([0-9]+)'
    })

    def __grant_recalc_start(self, trace, time, core, match, interests):
        for interest in interests:
            interest.tt_grant_recalc_start(trace, time, core)

    patterns.append({
        'name': 'grant_recalc_start',
        'regexp': 'homa_grant_recalc starting'
    })

    def __grant_check_start(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_grant_check_start(trace, time, core, id)

    patterns.append({
        'name': 'grant_check_start',
        'regexp': 'homa_grant_check_rpc starting for id ([0-9]+)'
    })

    def __grant_check_done(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_grant_check_done(trace, time, core, id)

    patterns.append({
        'name': 'grant_check_done',
        'regexp': 'homa_grant_check_rpc finished with id ([0-9]+)'
    })

    def __rpc_incoming(self, trace, time, core, match, interests):
        id = int(match.group(1))
        peer = match.group(2)
        received = int(match.group(3))
        length = int(match.group(4))
        for interest in interests:
            interest.tt_rpc_incoming(trace, time, core, id, peer, received, length)

    patterns.append({
        'name': 'rpc_incoming',
        'regexp': 'Incoming RPC id ([0-9]+), peer ([^,]+), ([0-9]+)/([0-9]+) bytes'
    })

    def __rpc_incoming2(self, trace, time, core, match, interests):
        id = int(match.group(1))
        incoming = int(match.group(2))
        granted = int(match.group(3))
        prio = int(match.group(4))
        for interest in interests:
            interest.tt_rpc_incoming2(trace, time, core, id, incoming,
                    granted, prio)

    patterns.append({
        'name': 'rpc_incoming2',
        'regexp': 'RPC id ([0-9]+) has incoming ([0-9]+), granted ([0-9]+), '
                'prio ([0-9]+)'
    })

    def __rpc_outgoing(self, trace, time, core, match, interests):
        id = int(match.group(1))
        sent = int(match.group(2))
        length = int(match.group(3))
        for interest in interests:
            interest.tt_rpc_outgoing(trace, time, core, id, sent, length)

    patterns.append({
        'name': 'rpc_outgoing',
        'regexp': 'Outgoing RPC id ([0-9]+), peer.*([0-9]+)/([0-9]+) bytes'
    })

    def __discard_unknown(self, trace, time, core, match, interests):
        id = int(match.group(1))
        for interest in interests:
            interest.tt_discard_unknown(trace, time, core, id)

    patterns.append({
        'name': 'discard_unknown',
        'regexp': 'Discarding packet for unknown RPC, id ([0-9]+),'
    })

#------------------------------------------------
# Analyzer: activity
#------------------------------------------------
class AnalyzeActivity:
    """
    Prints statistics about how many RPCs are live and data throughput.
    If --data is specified, generates activity_<node>.data files that
    describe activity over small intervals across the traces. The information
    in the .dat files includes new and total incoming messages, messages
    with grants, KB of outstanding grants, incoming data, and new and total
    outgoing messages.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')
        dispatcher.interest('AnalyzeGrants')
        dispatcher.interest('AnalyzeIntervals')

    def analyze(self):
        global rpcs, packets, traces

        # Each of the following lists contains <time, event> entries,
        # where event is 'start' or end'. The entry indicates that an
        # input or output message started arriving or completed at the given time.

        # Node name -> list of events for input messages on that server.
        self.node_in_msgs = {}

        # Node name -> list of events for output messages on that server.
        self.node_out_msgs = {}

        # Node name -> dictionary that maps from core number to total GRO data
        # received by that core
        self.node_core_in_bytes = {}

        # Node name -> total bytes output by that node
        self.node_out_bytes = {}

        # Node name -> dictionary that maps from peer address to the core
        # that handles incoming packets from that (sending) peer on this
        # (receiving) node.
        self.node_peer_cores = {}

        # Node name -> dictionary that maps from core number to total RPCs
        # still in incoming state at the end of the trace.
        self.node_core_pending = {}

        for node in get_sorted_nodes():
            self.node_in_msgs[node] = []
            self.node_out_msgs[node] = []
            self.node_core_in_bytes[node] = {}
            self.node_out_bytes[node] = 0
            self.node_peer_cores[node] = {}
            self.node_core_pending[node] = defaultdict(lambda: 0)

        # Scan RPCs to collect data
        for id, rpc in rpcs.items():
            node = rpc['node']

            if ('peer' in rpc) and ('gro_core' in rpc):
                self.node_peer_cores[node][rpc['peer']] = rpc['gro_core']

            if 'rx_live' in rpc:
                in_start, in_end = rpc['rx_live']
                self.node_in_msgs[node].append([in_start, 'start'])
                self.node_in_msgs[node].append([in_end, 'end'])

            if 'tx_live' in rpc:
                out_start, out_end = rpc['tx_live']
                self.node_out_msgs[node].append([out_start, 'start'])
                self.node_out_msgs[node].append([out_end, 'end'])

            sender_id = id^1
            if sender_id in rpcs:
                sender = rpcs[sender_id]
            else:
                sender = None
            for time, offset, prio in rpc['gro_data']:
                xmit = None
                if sender != None:
                    xmit = get_xmit_time(offset, sender)
                if (xmit == None) or (xmit > time):
                    xmit = time
                length = get_recv_length(offset, rpc['in_length'])
                if xmit > time:
                    print('\nNegative transmit time for offset %d in id %d: %s' %
                            (offset, id, rpc))
                    print('\nSending RPC: %s' % (sender))

                cores = self.node_core_in_bytes[node]
                core = rpc['gro_core']
                if not core in cores:
                    cores[core] = length
                else:
                    cores[core] += length

            for time, offset, length in rpc['send_data']:
                self.node_out_bytes[node] += length

        for rpc in rpcs.values():
            if 'remaining' in rpc:
                node = rpc['node']
                if not 'peer' in rpc:
                    print('RPC missing peer: %s' % (rpc))
                core = self.node_peer_cores[node][rpc['peer']]
                self.node_core_pending[node][core] += 1

    def sum_list(self, events):
        """
        Given a list of <time, event> entries where event is 'start' or 'end',
        return a list <num_starts, live_frac, avg_live>:
        num_starts:    Total number of 'start' events
        live_frac:     Fraction of all time when #starts > #ends
        avg_live:      Average value of #starts - #ends
        The input list should be sorted in order of time by the caller.
        """
        num_starts = 0
        cur_live = 0
        live_time = 0
        live_integral = 0
        last_time = events[0][0]

        for time, event in events:
            # print("%9.3f: %s, cur_live %d, live_time %.1f, live_integral %.1f" %
            #         (time, event, cur_live, live_time, live_integral))
            delta = time - last_time
            if cur_live:
                live_time += delta
            live_integral += delta * cur_live
            if event == 'start':
                num_starts += 1
                cur_live += 1
            else:
                cur_live -= 1
            last_time = time
        total_time = events[-1][0] - events[0][0]
        return num_starts, live_time/total_time, live_integral/total_time

    def output(self):
        global rpcs, traces

        def print_list(node, events, num_bytes, extra):
            global traces
            msgs, liveFrac, avgLive = self.sum_list(events)
            rate = msgs/(events[-1][0] - events[0][0])
            gbps = num_bytes*8e-3/(traces[node]['elapsed_time'])
            print('%-10s %6d %7.3f %9.3f %8.2f %7.2f  %7.2f%s' % (
                    node, msgs, rate, liveFrac, avgLive, gbps,
                    gbps/liveFrac, extra))

        print('\n-------------------')
        print('Analyzer: activity')
        print('-------------------\n')
        print('Msgs:          Total number of incoming/outgoing messages that were')
        print('               live at some point during the traces')
        print('MsgRate:       Rate at which new messages arrived (M/sec)')
        print('LiveFrac:      Fraction of time when at least one message was live')
        print('AvgLive:       Average number of live messages')
        print('Gbps:          Total message throughput (Gbps)')
        print('LiveGbps:      Total throughput when at least one message was live (Gbps)')
        print('MaxCore:       Highest incoming throughput via a single GRO core (Gbps)')
        print('MaxPend:       Highest # partially received RPCs (at end of trace) handled')
        print('               by a single GRO core')
        print('MaxPeers:      Highest number of peers serviced by a single GRO core\n')
        print('Incoming messages:')
        print('Node         Msgs MsgRate  LiveFrac  AvgLive    Gbps LiveGbps'
                '       MaxCore    MaxPend  MaxPeers')
        print('-------------------------------------------------------------'
                '-----------------------------------')
        for node in get_sorted_nodes():
            if not node in self.node_in_msgs:
                continue
            events = sorted(self.node_in_msgs[node], key=lambda t : t[0])
            max_core = 0
            max_bytes = 0
            total_bytes = 0
            for core, bytes in self.node_core_in_bytes[node].items():
                total_bytes += bytes
                if bytes > max_bytes:
                    max_bytes = bytes
                    max_core = core
            max_gbps = max_bytes*8e-3/(traces[node]['elapsed_time'])
            max_pending = -1
            max_pending_core = -1
            for core, pending in self.node_core_pending[node].items():
                if pending > max_pending:
                    max_pending = pending
                    max_pending_core = core
            core_peers = defaultdict(list)
            max_peers = -1
            max_peers_core = -1
            for peer, core in self.node_peer_cores[node].items():
                core_peers[core].append(peer)
                num_peers = len(core_peers[core])
                if num_peers > max_peers:
                    max_peers = num_peers
                    max_peers_core = core
            # print('core_peers for %s: %s' % (node, core_peers))
            extra = ' %7.2f (C%02d) %4d (C%02d) %3d (C%02d)' % (
                    max_gbps, max_core, max_pending, max_pending_core,
                    max_peers, max_peers_core)
            print_list(node, events, total_bytes, extra)
        print('\nOutgoing messages:')
        print('Node         Msgs MsgRate  LiveFrac  AvgLive    Gbps LiveGbps')
        print('-------------------------------------------------------------')
        for node in get_sorted_nodes():
            if not node in self.node_out_msgs:
                continue
            bytes = self.node_out_bytes[node]
            print_list(node, sorted(self.node_out_msgs[node]), bytes, "")

        if options.data:
            for node in get_sorted_nodes():
                f = open('%s/activity_%s.dat' % (options.data, node), 'w')
                f.write('# Node: %s\n' % (name))
                f.write('# Generated at %s.\n' %
                        (time.strftime('%I:%M %p on %m/%d/%Y')))
                f.write('# Statistics about RPC and packet activity on the ')
                f.write('node over %d usec\n' % (options.interval))
                f.write('# intervals:\n')
                f.write('# Time:       End of the time interval\n')
                f.write('# NewRx:      New incoming messages that started during '
                        'the interval\n')
                f.write('# NumRx:      Incoming messages that were partially '
                        'received at the\n')
                f.write('#             end of the interval\n')
                f.write('# RxGts:      Number of incoming RPCS with outstanding grants at the\n')
                f.write('#             end of the interval (doesn\'t include unscheduled)\n')
                f.write('# RxGtKB:     Number of KB for which grants have been sent but data\n')
                f.write('              not yet received at the end of the interval\n')
                f.write('# RxPkts:     Number of data packets received during the interval\n')
                f.write('# RxGbps:     Throughput of received data during the interval\n')
                f.write('# Incoming:   KB of data that had been transmitted but not yet\n')
                f.write('#             received, as of the end of the interval\n')
                f.write('# NewTx:      New outgoing messages that started during '
                        'the interval\n')
                f.write('# NumTx:      Outgoing messages that were partially '
                        'transmitted at the\n')
                f.write('#             end of the interval\n')
                f.write('\n')
                f.write('#   Time NewRx NumRx RxGts RxGtKB RxPkts RxGbps Incoming NewTx NumTx\n')
                for interval in intervals[node]:
                    f.write('%8.1f' % (interval['time']))
                    if 'rx_starts' in interval:
                        f.write(' %5d %5d' % (interval['rx_starts'],
                                interval['rx_live']))
                    else:
                        f.write(' '*12)
                    if 'rx_grants' in interval:
                        f.write(' %5d %6.0f' % (interval['rx_grants'],
                            interval['rx_grant_bytes']/1000.0))
                    else:
                        f.write(' ' *13)
                    if 'rx_pkts' in interval:
                        f.write(' %6d %6.1f   %6.1f' % (interval['rx_pkts'],
                                gbps(interval['rx_bytes'], options.interval),
                                (interval['rx_data_qdisc']
                                 + interval['rx_data_net'])*1e-3))
                    else:
                        f.write(' '*12)
                    if 'tx_starts' in interval:
                        f.write(' %5d %5d' % (interval['tx_starts'],
                                interval['tx_live_req']
                                + interval['tx_live_resp']))
                    else:
                        f.write(' '*12)
                    f.write('\n')
                f.close()

#------------------------------------------------
# Analyzer: bpages
#------------------------------------------------
class AnalyzeBpages:
    """
    Output information about bpage usage on each of the nodes, as of the
    end of the traces. This information is only available if
    homa_rpc_log_active_tt was invoked before freezing the time traces.
    """
    def __init__(self, dispatcher):
        self.node_rpcs = defaultdict(lambda : 0)
        self.node_bpages = defaultdict(lambda: 0)

    def tt_bpages_alloced(self, trace, time, core, id, bpages):
        node = trace['node']
        self.node_rpcs[node] += 1
        self.node_bpages[node] += bpages

    def output(self):
        global traces, options
        print('\n-------------------')
        print('Analyzer: bpages')
        print('-------------------')
        print('Bpage usage at the end of the traces')
        print('Node:    Name of a node')
        print('RPCs:    Number of RPCs on that node with bpages allocated')
        print('Bpages:  Total bpages (or partial bpages) allocated on the node')
        print('')
        print('Node         RPCs  Bpages')
        for node in get_sorted_nodes():
            print('%-10s  %5d  %6d' % (node, self.node_rpcs[node],
                    self.node_bpages[node]))

#------------------------------------------------
# Analyzer: copy
#------------------------------------------------
class AnalyzeCopy:
    """
    Measures the throughput of copies between user space and kernel space.
    """

    def __init__(self, dispatcher):
        return

    def init_trace(self, trace):
        trace['copy'] = {
            # Keys are cores; values are times when most recent copy from
            # user space started on that core
            'in_start': {},

            # Total bytes of data copied from user space for large messages
            'large_in_data': 0,

            # Total microseconds spent copying data for large messages
            'large_in_time': 0.0,

            # Total number of large messages copied into kernel
            'large_in_count': 0,

            # List of copy times for messages no larger than 1200 B
            'small_in_times': [],

            # Total time spent copying in data for all messages
            'total_in_time': 0.0,

            # Keys are cores; values are times when most recent copy to
            # user space started on that core
            'out_start': {},

            # Keys are cores; values are times when most recent copy to
            # user space ended on that core
            'out_end': {},

            # Keys are cores; values are sizes of last copy to user space
            'out_size': {},

            # Total bytes of data copied to user space for large messages
            'large_out_data': 0,

            # Total microseconds spent copying data for large messages
            'large_out_time': 0.0,

            # Total microseconds spent copying data for large messages,
            # including time spent freeing skbs.
            'large_out_time_with_skbs': 0.0,

            # Total number of large messages copied out of kernel
            'large_out_count': 0,

            # List of copy times for messages no larger than 1200 B
            'small_out_times': [],

            # Total time spent copying out data for all messages
            'total_out_time': 0.0,

            # Total number of skbs freed after copying data to user space
            'skbs_freed': 0,

            # Total time spent freeing skbs after copying data
            'skb_free_time': 0.0
        }

    def tt_copy_in_start(self, trace, time, core):
        stats = trace['copy']
        stats['in_start'][core] = time

    def tt_copy_in_done(self, trace, time, core, id, num_bytes):
        global options
        stats = trace['copy']
        if core in stats['in_start']:
            delta = time - stats['in_start'][core]
            stats['total_in_time'] += delta
            if num_bytes <= 1000:
                stats['small_in_times'].append(delta)
            elif num_bytes >= 5000:
                stats['large_in_data'] += num_bytes
                stats['large_in_time'] += delta
                stats['large_in_count'] += 1
            if 0 and options.verbose:
                print('%9.3f Copy in finished [C%02d]: %d bytes, %.1f us, %5.1f Gbps' %
                        (time, core, num_bytes, delta, 8e-03*num_bytes/delta))

    def tt_copy_out_start(self, trace, time, core, id):
        stats = trace['copy']
        stats['out_start'][core] = time

    def tt_copy_out_done(self, trace, time, core, id, start, end):
        global options
        stats = trace['copy']
        num_bytes = end - start
        if core in stats['out_start']:
            stats['out_end'][core] = time
            stats['out_size'][core] = num_bytes
            delta = time - stats['out_start'][core]
            stats['out_start'][core] = time
            stats['total_out_time'] += delta
            if num_bytes <= 1000:
                stats['small_out_times'].append(delta)
            elif num_bytes >= 5000:
                stats['large_out_data'] += num_bytes
                stats['large_out_time'] += delta
                stats['large_out_time_with_skbs'] += delta
                stats['large_out_count'] += 1
            if 0 and options.verbose:
                print('%9.3f Copy out finished [C%02d]: %d bytes, %.1f us, %5.1f Gbps' %
                        (time, core, num_bytes, delta, 8e-03*num_bytes/delta))

    def tt_free_skbs(self, trace, time, core, num_skbs):
        stats = trace['copy']
        if core in stats['out_end']:
            delta = time - stats['out_end'][core]
            stats['skbs_freed'] += num_skbs
            stats['skb_free_time'] += delta
            if stats['out_size'][core] >= 5000:
                stats['large_out_time_with_skbs'] += delta

    def output(self):
        global traces
        print('\n---------------')
        print('Analyzer: copy')
        print('---------------')
        print('Performance of data copying between user space and kernel:')
        print('Node:     Name of node')
        print('#Short:   Number of short blocks copied (<= 1000 B)')
        print('Min:      Minimum copy time for a short block (usec)')
        print('P50:      Median copy time for short blocks (usec)')
        print('P90:      90th percentile copy time for short blocks (usec)')
        print('P99:      99th percentile copy time for short blocks (usec)')
        print('Max:      Maximum copy time for a short block (usec)')
        print('Avg:      Average copy time for short blocks (usec)')
        print('#Long:    Number of long blocks copied (>= 5000 B)')
        print('TputC:    Average per-core throughput for copying long blocks')
        print('          when actively copying (Gbps)')
        print('TputN:    Average long block copy throughput for the node (Gbps)')
        print('Cores:    Average number of cores copying long blocks')
        print('')
        print('Copying from user space to kernel:')
        print('Node       #Short   Min   P50   P90   P99   Max   Avg  #Long  '
                'TputC TputN Cores')
        print('--------------------------------------------------------------'
                '-----------------')
        for node in get_sorted_nodes():
            trace = traces[node]
            stats = trace['copy']

            num_short = len(stats['small_in_times'])
            if num_short == 0:
                min = p50 = p90 = p99 = max = avg = 0.0
            else:
                sorted_data = sorted(stats['small_in_times'])
                min = sorted_data[0]
                p50 = sorted_data[50*num_short//100]
                p90 = sorted_data[90*num_short//100]
                p99 = sorted_data[99*num_short//100]
                max = sorted_data[-1]
                avg = sum(sorted_data)/num_short

            num_long = stats['large_in_count']
            if stats['large_in_time'] == 0:
                core_tput = '   N/A'
                node_tput = '   N/A'
                cores = 0
            else:
                core_tput = '%6.1f' % (8e-03*stats['large_in_data']
                            /stats['large_in_time'])
                node_tput = '%6.1f' % (8e-03*stats['large_in_data']
                            /trace['elapsed_time'])
                cores = stats['total_in_time']/trace['elapsed_time']
            print('%-10s %6d%6.1f%6.1f%6.1f%6.1f%6.1f%6.1f  %5d %s%s %5.2f' %
                    (node, num_short, min, p50, p90, p99, max, avg, num_long,
                    core_tput, node_tput, cores))

        print('\nCopying from kernel space to user:')
        print('Node       #Short   Min   P50   P90   P99   Max   Avg  #Long  '
                'TputC TputN Cores')
        print('--------------------------------------------------------------'
                '-----------------')
        for node in get_sorted_nodes():
            trace = traces[node]
            stats = trace['copy']

            num_short = len(stats['small_out_times'])
            if num_short == 0:
                min = p50 = p90 = p99 = max = avg = 0.0
            else:
                sorted_data = sorted(stats['small_out_times'])
                min = sorted_data[0]
                p50 = sorted_data[50*num_short//100]
                p90 = sorted_data[90*num_short//100]
                p99 = sorted_data[99*num_short//100]
                max = sorted_data[-1]
                avg = sum(sorted_data)/num_short

            num_long = stats['large_out_count']
            if stats['large_out_time'] == 0:
                core_tput = '   N/A'
                node_tput = '   N/A'
                cores = 0
            else:
                core_tput = '%6.1f' % (8e-03*stats['large_out_data']
                            /stats['large_out_time'])
                node_tput = '%6.1f' % (8e-03*stats['large_out_data']
                            /trace['elapsed_time'])
                cores = stats['total_out_time']/trace['elapsed_time']
            print('%-10s %6d%6.1f%6.1f%6.1f%6.1f%6.1f%6.1f  %5d %s%s %5.2f' %
                    (node, num_short, min, p50, p90, p99, max, avg, num_long,
                    core_tput, node_tput, cores))

        print('\nImpact of freeing socket buffers while copying to user:')
        print('Node:     Name of node')
        print('#Freed:   Number of skbs freed')
        print('Time:     Average time to free an skb (usec)')
        print('Tput:     Effective kernel->user throughput per core (TputC) including')
        print('          skb freeing (Gbps)')
        print('')
        print('Node       #Freed   Time   Tput')
        print('-------------------------------')
        for node in get_sorted_nodes():
            trace = traces[node]
            stats = trace['copy']
            stats['skbs_freed']
            if stats['skbs_freed'] == 0:
                free_time = 0
                tput = 0
            else:
                free_time = stats['skb_free_time']/stats['skbs_freed']
                if stats['large_out_time_with_skbs']:
                    tput = '%6.1f' % (8e-03*stats['large_out_data']
                        /stats['large_out_time_with_skbs'])
                else:
                    tput = '   N/A'
            print('%-10s %6d %6.2f %s' % (node, stats['skbs_freed'],
                    free_time, tput))

#------------------------------------------------
# Analyzer: core
#------------------------------------------------
class AnalyzeCore:
    """
    Generates statistics about activity on a single core. Requires the --node,
    --core, and --data options.
    """

    def __init__(self, dispatcher):
        global options
        require_options('core', 'data', 'node')

        # List of all intervals over the life of the trace, each list entry
        # is a dictionary with the following values related to that interval:
        # time:          Ending time of the interval
        # gro_data:      Number of incoming data packets processed by GRO
        # gro_grant:     Number of incoming grant packets processed by GRO
        # softirq_data:  Number of incoming data packets processed by SoftIRQ
        # softirq_grant: Number of incoming grant packets processed by SoftIRQ
        # resends:       Number of incoming resend requests processed by SoftIRQ
        # busy:          Number of BUSY packets sent
        # grant_sends:   Number of GRANT packets sent
        self.intervals = []

    def init_trace(self, trace):
        # Target core id -> list of times when gro chose that core but
        # SoftIRQ hasn't yet woken up
        self.gro_handoffs = defaultdict(list)

    def get_interval(self, t):
        """
        Find the interval corresponding to time t, initializing new intervals
        when needed.
        """
        global options

        interval_length = options.interval
        while True:
            if len(self.intervals) == 0:
                end = int(t)//interval_length * interval_length
                self.intervals.append({'time': end})
                interval = self.intervals[0]
            else:
                first_end = self.intervals[0]['time']
                i = int((t - (first_end - interval_length))//interval_length)
                if i < 0:
                    raise Exception('Unexpected index %d (time %9.1f) in '
                            'AnalyzeCore.get_interval' % (i, t))
                if i < len(self.intervals):
                    return self.intervals[i]
                self.intervals.append({'time': first_end
                        + interval_length*len(self.intervals)})
                interval = self.intervals[-1]
            interval['gro_data']= 0
            interval['gro_grant']= 0
            interval['softirq_data'] = 0
            interval['softirq_grant'] = 0
            interval['resends'] = 0
            interval['busy'] = 0
            interval['grant_sends'] = 0

    def inc_counter(self, trace, time, core, name):
        """
        Does most of the work of the tt_* methods below: increment the
        counter given by name if the record is for the right core.
        """
        global options
        if (core != options.core) or (trace['node'] != options.node):
            return
        self.get_interval(time)[name] += 1

    def tt_gro_data(self, trace, time, core, peer, id, offset, prio):
        self.inc_counter(trace, time, core, 'gro_data')

    def tt_gro_grant(self, trace, time, core, peer, id, offset, prio):
        self.inc_counter(trace, time, core, 'gro_grant')

    def tt_softirq_data(self, trace, time, core, id, offset, msg_length):
        self.inc_counter(trace, time, core, 'softirq_data')

    def tt_softirq_grant(self, trace, time, core, id, offset, priority,
            increment):
        self.inc_counter(trace, time, core, 'softirq_grant')

    def tt_softirq_resend(self, trace, time, core, id, offset, length, prio):
        self.inc_counter(trace, time, core, 'resends')

    def tt_resend_busy(self, trace, time, core, id, state):
        self.inc_counter(trace, time, core, 'busy')

    def tt_send_grant(self, trace, time, core, id, offset, priority, increment):
        self.inc_counter(trace, time, core, 'grant_sends')

    def output(self):
        global options

        print('\n-------------------')
        print('Analyzer: core')
        print('-------------------')

        if not self.intervals:
            print('No data found for core %d on %s' % (options.core,
                    options.node))
            return

        print('\nOverall statistics:')
        print('                                      Total Avg/Interval')
        l = len(self.intervals)
        total = sum_fields(self.intervals, 'softirq_data')
        print('Data packets processed by SoftIRQ:   %6d   %6.1f' %
                (total, total/l))
        total = sum_fields(self.intervals, 'softirq_grant')
        print('Grants processed by SoftIRQ:         %6d   %6.1f' %
                (total, total/l))
        total = sum_fields(self.intervals, 'resends')
        print('Resends processed by SoftIRQ:        %6d   %6.1f' %
                (total, total/l))
        total = sum_fields(self.intervals, 'busy')
        print('BUSY packets sent:                   %6d   %6.1f' %
                (total, total/l))
        total = sum_fields(self.intervals, 'grant_sends')
        print('GRANT packets sent:                  %6d   %6.1f' %
                (total, total/l))

        f = open('%s/core_%s-%d.dat' % (options.data, options.node,
                options.core), 'w')
        f.write('# Node: %s\n' % (options.node))
        f.write('# Core: %d\n' % (options.core))
        f.write('# Generated at %s.\n' %
                (time.strftime('%I:%M %p on %m/%d/%Y')))
        f.write('#\n')
        f.write('# Statistics about activity on core %d of %s over %d usec '
                'intervals:\n' % (options.core, options.node,
                options.interval))
        f.write('# Time:      End of the time interval\n')
        f.write('# GroD:      Data packets processed by GRO\n')
        f.write('# GroG:      Grant packets processed by GRO\n')
        f.write('# SoftD:     Data packets processed by SoftIRQ\n')
        f.write('# SoftG:     Grant packets processed by SoftIRQ\n');
        f.write('# SoftR:     RESEND requests processed by SoftIRQ\n');
        f.write('# TxBusy:    BUSY packets sent\n');
        f.write('# TxGrant:   GRANT packets sent\n');

        f.write('\n    Time  GroD  GroG SoftD  SoftG SoftR TxBusy TxGrant\n')
        total = 0
        for interval in self.intervals:
            if not 'softirq_data' in interval:
                print('Interval: %s' % (interval))
            f.write('%8.1f %5d %5d %5d  %5d %5d  %5d   %5d\n'
                    % (interval['time'], interval['gro_data'],
                    interval['gro_grant'], interval['softirq_data'],
                    interval['softirq_grant'], interval['resends'],
                    interval['busy'], interval['grant_sends']))

#------------------------------------------------
# Analyzer: coregaps
#------------------------------------------------
class AnalyzeCoregaps:
    """
    Analyzes events on all cores to identify long gaps (periods of time
    where there were no trace records for an individual core).
    """

    def __init__(self, dispatcher):

        # node -> dictionary mapping core -> time of most recent
        # event on that core.
        self.last_event = {}

        # node -> list of <node, core, start, length> tuples: node and core
        # identify a particular core, and start and length describe
        # a gap where that core was idle.
        self.node_gaps = {}

        self.gap_threshold = 5000

    def init_trace(self, trace):
        self.cur_node = {}
        self.last_event[trace['node']] = self.cur_node
        self.node_gaps[trace['node']] = []

    def tt_all(self, trace, t, core, msg):
        if core in self.cur_node:
            gap = t - self.cur_node[core]
            if gap > self.gap_threshold:
                self.node_gaps[trace['node']].append([trace['node'], core,
                        self.cur_node[core], gap])
        self.cur_node[core] = t

    def output(self):
        global options
        max_per_node = 5

        print('\n-------------------')
        print('Analyzer: coregaps')
        print('-------------------')
        print('')
        print('Longest time intervals where no timetrace events were recorded')
        print('for a core (limit: %d gaps per node):' % (max_per_node))
        print('')

        # Identify gaps that occurred at the end of the traces (no
        # terminating record for the gap).
        for node, core_times in self.last_event.items():
            trace_last = traces[node]['last_time']
            for core in core_times.keys():
                gap = trace_last - core_times[core]
                if gap > self.gap_threshold:
                    self.node_gaps[node].append([node, core, core_times[core], gap])

        if len(self.node_gaps) == 0:
            print('There were no gaps longer than %.1f ms' %
                    (self.gap_threshold/1000))
            return

        print('Node       Core     Start   Length (ms)')
        for node in get_sorted_nodes():
            if not node in self.last_event:
                continue
            node_cores = self.last_event[node]
            gaps = sorted(self.node_gaps[node], key=lambda t : t[3],
                    reverse=True)
            count = 0
            for gap in gaps:
                if len(gap) != 4:
                    print('Bad gap: %s' % (gap))
            for gap_node, core, start, length in gaps:
                print('%-10s %4d %9.1f   %6.1f' % (gap_node, core, start,
                        length/1000))
                count += 1
                if count >= max_per_node:
                    break

#------------------------------------------------
# Analyzer: delay
#------------------------------------------------
class AnalyzeDelay:
    """
    Prints information about various delays, including delays associated
    with packets at various stages and delays in waking up threads. With
    --verbose, prints information about specific instances of long delays.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')

        # <delay, end time> for gro->softirq handoffs
        self.softirq_wakeups = []

        # RPC id -> time when homa_rpc_handoff handed off that RPC to a thread.
        self.rpc_handoffs = {}

        # RPC id -> time when homa_rpc_handoff queued the RPC.
        self.rpc_queued = {}

        # <delay, end time, node> for softirq->app handoffs (thread was polling)
        self.app_poll_wakeups = []

        # <delay, end time, node> for softirq->app handoffs (thread was sleeping)
        self.app_sleep_wakeups = []

        # <delay, end time, node> for softirq->app handoffs when RPC was queued
        self.app_queue_wakeups = []

        # An entry exists for RPC id if a handoff occurred while a
        # thread was polling
        self.poll_success = {}

    def init_trace(self, trace):
        # Target core id -> list of times when gro chose that core but
        # SoftIRQ hasn't yet woken up
        self.gro_handoffs = defaultdict(list)

    def tt_gro_handoff(self, trace, time, core, softirq_core):
        self.gro_handoffs[softirq_core].append(time)

    def tt_softirq_start(self, trace, time, core):
        if not self.gro_handoffs[core]:
            return
        self.softirq_wakeups.append([time - self.gro_handoffs[core][0], time,
                trace['node']])
        self.gro_handoffs[core].pop(0)

    def tt_rpc_handoff(self, trace, time, core, id):
        if id in self.rpc_handoffs:
            print('Multiple RPC handoffs for id %s on %s: %9.3f and %9.3f' %
                    (id, trace['node'], self.rpc_handoffs[id], time),
                    file=sys.stderr)
        self.rpc_handoffs[id] = time

    def tt_poll_success(self, trace, time, core, id):
        self.poll_success[id] = time

    def tt_rpc_queued(self, trace, time, core, id):
        self.rpc_queued[id] = time

    def tt_wait_found_rpc(self, trace, time, core, id):
        if id in self.rpc_handoffs:
            delay = time - self.rpc_handoffs[id]
            if id in self.poll_success:
                self.app_poll_wakeups.append([delay, time, trace['node']])
                del self.poll_success[id]
            else:
                self.app_sleep_wakeups.append([delay, time, trace['node']])
            del self.rpc_handoffs[id]
        elif id in self.rpc_queued:
            self.app_queue_wakeups.append([time - self.rpc_queued[id], time,
                    trace['node']])
            del self.rpc_queued[id]

    def print_pkt_delays(self):
        """
        Prints basic packet delay info, returns verbose output for optional
        printing by caller.
        """
        global packets, grants, options

        # Each of the following lists holds <delay, pkt_id, time> tuples for
        # a particular stage of a packet's lifetime, where delay is the
        # delay through that stage, pkt_id identifies the packet (rpc_id:offset)
        # and time is when the delay ended.
        short_to_nic = []
        short_to_gro = []
        short_to_softirq = []
        short_total = []

        long_to_nic = []
        long_to_gro = []
        long_to_softirq = []
        long_total = []

        grant_to_nic = []
        grant_to_gro = []
        grant_to_softirq = []
        grant_total = []

        # Collect statistics about delays within individual packets.
        mtu = get_mtu()
        for p, pkt in packets.items():
            if (pkt['msg_length'] != None) and (pkt['msg_length'] <= mtu):
                if ('xmit' in pkt) and ('nic' in pkt):
                    delay = pkt['nic'] - pkt['xmit']
                    if delay > 0:
                        short_to_nic.append([delay, p, pkt['nic']])
                if ('nic' in pkt) and ('gro' in pkt):
                    delay = pkt['gro'] - pkt['nic']
                    if delay > 0:
                        short_to_gro.append([delay, p, pkt['gro']])
                if ('gro' in pkt) and ('softirq' in pkt):
                    delay = pkt['softirq'] - pkt['gro']
                    if delay > 0:
                        short_to_softirq.append([delay, p, pkt['softirq']])
                if ('softirq' in pkt) and ('xmit' in pkt):
                    delay = pkt['softirq'] - pkt['xmit']
                    if delay > 0:
                        short_total.append([delay, p, pkt['softirq']])
            else:
                if ('xmit' in pkt) and ('nic' in pkt):
                    delay = pkt['nic'] - pkt['xmit']
                    if delay > 0:
                        long_to_nic.append([delay, p, pkt['nic']])
                if ('nic' in pkt) and ('gro' in pkt):
                    delay = pkt['gro'] - pkt['nic']
                    if delay > 0:
                        long_to_gro.append([delay, p, pkt['gro']])
                if ('gro' in pkt) and ('softirq' in pkt):
                    delay = pkt['softirq'] - pkt['gro']
                    if delay > 0:
                        long_to_softirq.append([delay, p, pkt['softirq']])
                if ('softirq' in pkt) and ('xmit' in pkt):
                    delay = pkt['softirq'] - pkt['xmit']
                    if delay > 0:
                        long_total.append([delay, p, pkt['softirq']])

        for p, pkt in grants.items():
            if ('xmit' in pkt) and ('nic' in pkt):
                delay = pkt['nic'] - pkt['xmit']
                if delay > 0:
                    grant_to_nic.append([delay, p, pkt['nic']])
            if ('nic' in pkt) and ('gro' in pkt):
                delay = pkt['gro'] - pkt['nic']
                if delay > 0:
                    grant_to_gro.append([delay, p, pkt['gro']])
            if ('gro' in pkt) and ('softirq' in pkt):
                delay = pkt['softirq'] - pkt['gro']
                if delay > 0:
                    grant_to_softirq.append([delay, p, pkt['softirq']])
            if ('softirq' in pkt) and ('xmit' in pkt):
                delay = pkt['softirq'] - pkt['xmit']
                if delay > 0:
                    grant_total.append([delay, p, pkt['softirq']])

        print('\n----------------')
        print('Analyzer: delay')
        print('----------------')
        print('Delays in the transmission and processing of data and grant packets')
        print('(all times in usecs):')
        print('Xmit:     Time from ip*xmit call until driver queued packet for NIC')
        print('          (for grants, includes time in homa_send_grants and ')
        print('          homa_xmit_control)')
        print('Net:      Time from when NIC received packet until GRO started processing')
        print('SoftIRQ:  Time from GRO until SoftIRQ started processing')
        print('Total:    Total time from ip*xmit call until SoftIRQ processing')

        def print_pcts(data, label):
            data.sort(key=lambda t : t[0])
            if not data:
                print('%-10s      0' % (label))
            else:
                print('%-10s %6d %6.1f %6.1f %6.1f %6.1f %6.1f %6.1f %6.1f' % (label,
                    len(data), data[0][0], data[10*len(data)//100][0],
                    data[50*len(data)//100][0], data[90*len(data)//100][0],
                    data[99*len(data)//100][0], data[len(data)-1][0],
                    list_avg(data, 0)))
        print('\nPhase        Count   Min    P10    P50    P90    P99    Max    Avg')
        print('-------------------------------------------------------------------------')
        print('Data packets from single-packet messages:')
        print_pcts(short_to_nic, 'Xmit')
        print_pcts(short_to_gro, 'Net')
        print_pcts(short_to_softirq, 'SoftIRQ')
        print_pcts(short_total, 'Total')

        print('\nData packets from multi-packet messages:')
        print_pcts(long_to_nic, 'Xmit')
        print_pcts(long_to_gro, 'Net')
        print_pcts(long_to_softirq, 'SoftIRQ')
        print_pcts(long_total, 'Total')

        print('\nGrants:')
        print_pcts(grant_to_nic, 'Xmit')
        print_pcts(grant_to_gro, 'Net')
        print_pcts(grant_to_softirq, 'SoftIRQ')
        print_pcts(grant_total, 'Total')

        # Handle --verbose for packet-related delays.
        def print_worst(data, label):
            global rpcs

            # The goal is to print about 20 packets covering the 98th-100th
            # percentiles; we'll print one out of every "interval" packets.
            result = ''
            num_pkts = len(data)
            interval = num_pkts//(50*20)
            if interval == 0:
                interval = 1
            for i in range(num_pkts-1, num_pkts - 20*interval, -interval):
                if i < 0:
                    break
                pkt = data[i]
                recv_id = int(pkt[1].split(':')[0]) ^ 1
                dest = '      ????   ??'
                if recv_id in rpcs:
                    rpc = rpcs[recv_id]
                    if 'gro_core' in rpc:
                        dest = '%10s %4d' % (rpc['node'], rpc['gro_core'])
                    else:
                        dest = '%10s   ??' % (rpc['node'])
                result += '%-8s %6.1f  %20s %s %9.3f %5.1f\n' % (label, pkt[0],
                        pkt[1], dest, pkt[2], i*100/num_pkts)
            return result

        verbose = 'Sampled packets with outlier delays:\n'
        verbose += 'Phase:    Phase of delay: Xmit, Net, or SoftIRQ\n'
        verbose += 'Delay:    Delay for this phase\n'
        verbose += 'Packet:   Sender\'s identifier for packet: rpc_id:offset\n'
        verbose += 'Node:     Node where packet was received\n'
        verbose += 'Core:     Core where homa_gro_receive processed packet\n'
        verbose += 'EndTime:  Time when phase completed\n'
        verbose += 'Pctl:     Percentile of this packet\'s delay\n\n'
        verbose += ('Phase   Delay (us)             Packet   RecvNode Core   '
                'EndTime  Pctl\n')
        verbose += ('--------------------------------------------------------'
                '-------------\n')

        verbose += 'Data packets from single-packet messages:\n'
        verbose += print_worst(short_to_nic, 'Xmit')
        verbose += print_worst(short_to_gro, 'Net')
        verbose += print_worst(short_to_softirq, 'SoftIRQ')
        verbose += print_worst(short_total, 'Total')

        verbose += '\nData packets from multi-packet messages:\n'
        verbose += print_worst(long_to_nic, 'Xmit')
        verbose += print_worst(long_to_gro, 'Net')
        verbose += print_worst(long_to_softirq, 'SoftIRQ')
        verbose += print_worst(long_total, 'Total')

        verbose += '\nGrants:\n'
        verbose += print_worst(grant_to_nic, 'Xmit')
        verbose += print_worst(grant_to_gro, 'Net')
        verbose += print_worst(grant_to_softirq, 'SoftIRQ')
        verbose += print_worst(grant_total, 'Total')

        # Redo the statistics gathering, but only include the worst packets
        # from each category.
        if short_total:
            min_short = short_total[98*len(short_total)//100][0]
            max_short = short_total[99*len(short_total)//100][0]
        else:
            min_short = 0
            max_short = 0
        if long_total:
            min_long = long_total[98*len(long_total)//100][0]
            max_long = long_total[99*len(long_total)//100][0]
        else:
            min_long = 0
            max_long = 0
        if grant_total:
            min_grant = grant_total[98*len(grant_total)//100][0]
            max_grant = grant_total[99*len(grant_total)//100][0]
        else:
            min_grant = 0
            max_grant = 0

        short_to_nic = []
        short_to_gro = []
        short_to_softirq = []

        long_to_nic = []
        long_to_gro = []
        long_to_softirq = []

        grant_to_nic = []
        grant_to_gro = []
        grant_to_softirq = []

        for p, pkt in packets.items():
            if (not 'softirq' in pkt) or (not 'xmit' in pkt):
                continue
            total = pkt['softirq'] - pkt['xmit']
            if (pkt['msg_length'] != None) and (pkt['msg_length'] <= mtu):
                if (total < min_short) or (total > max_short):
                    continue;
                if ('xmit' in pkt) and ('nic' in pkt):
                    short_to_nic.append(
                            [pkt['nic'] - pkt['xmit'], p, pkt['nic']])
                if ('nic' in pkt) and ('gro' in pkt):
                    short_to_gro.append(
                            [pkt['gro'] - pkt['nic'], p, pkt['gro']])
                if ('gro' in pkt) and ('softirq' in pkt):
                    short_to_softirq.append(
                            [pkt['softirq'] - pkt['gro'], p, pkt['softirq']])
            else:
                if (total < min_long) or (total > max_long):
                    continue;
                if ('xmit' in pkt) and ('nic' in pkt):
                    long_to_nic.append(
                            [pkt['nic'] - pkt['xmit'], p, pkt['nic']])
                if ('nic' in pkt) and ('gro' in pkt):
                    long_to_gro.append(
                            [pkt['gro'] - pkt['nic'], p, pkt['gro']])
                if ('gro' in pkt) and ('softirq' in pkt):
                    long_to_softirq.append(
                            [pkt['softirq'] - pkt['gro'], p, pkt['softirq']])

        for pkt in grants.values():
            if (not 'softirq' in pkt) or (not 'xmit' in pkt):
                continue
            total = pkt['softirq'] - pkt['xmit']
            if (total < min_grant) or (total > max_grant):
                continue;
            if ('xmit' in pkt) and ('nic' in pkt):
                grant_to_nic.append(
                        [pkt['nic'] - pkt['xmit'], p, pkt['nic']])
            if ('nic' in pkt) and ('gro' in pkt):
                grant_to_gro.append(
                        [pkt['gro'] - pkt['nic'], p, pkt['gro']])
            if ('gro' in pkt) and ('softirq' in pkt):
                grant_to_softirq.append(
                        [pkt['softirq'] - pkt['gro'], p, pkt['softirq']])

        def get_slow_summary(data):
            if not data:
                return " "*13
            data.sort(key=lambda t : t[0])
            return '%6.1f %6.1f' % (data[50*len(data)//100][0],
                    list_avg(data, 0))

        print('\nPhase breakdown for P98-P99 packets:')
        print('                          Xmit          Net         SoftIRQ')
        print('               Pkts    P50    Avg    P50    Avg    P50    Avg')
        print('-------------------------------------------------------------')
        print('Single-packet %5d %s %s %s' % (len(short_to_nic),
                get_slow_summary(short_to_nic),
                get_slow_summary(short_to_gro),
                get_slow_summary(short_to_softirq)))
        print('Multi-packet  %5d %s %s %s' % (len(long_to_nic),
                get_slow_summary(long_to_nic),
                get_slow_summary(long_to_gro),
                get_slow_summary(long_to_softirq)))
        print('Grants        %5d %s %s %s' % (len(grant_to_nic),
                get_slow_summary(grant_to_nic),
                get_slow_summary(grant_to_gro),
                get_slow_summary(grant_to_softirq)))
        return verbose

    def print_wakeup_delays(self):
        """
        Prints basic info about thread wakeup delays, returns verbose output
        for optional printing by caller.
        """
        global options

        soft = self.softirq_wakeups
        soft.sort()
        app_poll = self.app_poll_wakeups
        app_poll.sort()
        app_sleep = self.app_sleep_wakeups
        app_sleep.sort()
        app_queue = self.app_queue_wakeups
        app_queue.sort()
        print('\nDelays in handing off from one core to another:')
        print('                            Count   Min    P10    P50    P90    P99    '
                'Max    Avg')
        print('------------------------------------------------------------'
                '---------------------')

        def print_percentiles(label, data):
            num = len(data)
            if num == 0:
                print('%-26s %6d' % (label, 0))
            else:
                print('%-26s %6d %5.1f %6.1f %6.1f %6.1f %6.1f %6.1f %6.1f'
                    % (label, num, data[0][0], data[10*num//100][0],
                    data[50*num//100][0], data[90*num//100][0],
                    data[99*num//100][0], data[num-1][0], list_avg(data, 0)))
        print_percentiles('GRO to SoftIRQ:', soft)
        print_percentiles('SoftIRQ to polling app:', app_poll)
        print_percentiles('SoftIRQ to sleeping app:', app_sleep)
        print_percentiles('SoftIRQ to app via queue:', app_queue)

        verbose = 'Worst-case handoff delays:\n'
        verbose += 'Type                   Delay (us)    End Time       Node  Pctl\n'
        verbose += '--------------------------------------------------------------\n'

        def print_worst(label, data):
            # The goal is to print about 10 records covering the 98th-100th
            # percentiles; we'll print one out of every "interval" packets.
            num = len(data)
            interval = num//(50*10)
            if interval == 0:
                interval = 1
            result = ''
            for i in range(num-1, num - 10*interval, -interval):
                if i < 0:
                    break
                time, delay, node = data[i]
                result += '%-26s %6.1f   %9.3f %10s %5.1f\n' % (
                        label, time, delay, node, 100*i/(num-1))
            return result

        verbose += print_worst('GRO to SoftIRQ', soft)
        verbose += print_worst('SoftIRQ to polling app', app_poll)
        verbose += print_worst('SoftIRQ to sleeping app', app_sleep)
        verbose += print_worst('SoftIRQ to app via queue', app_queue)
        return verbose

    def output(self):
        global options

        delay_verbose = self.print_pkt_delays()
        wakeup_verbose = self.print_wakeup_delays()
        if options.verbose:
            print('')
            print(delay_verbose, end='')
            print('')
            print(wakeup_verbose, end='')

#------------------------------------------------
# Analyzer: filter
#------------------------------------------------
class AnalyzeFilter:
    """
    Prints information about the packets selected by the following command-line
    options: --tx-node, --tx-core, --tx-start, --tx-end, --rx-node, --rx-core,
    --rx-start, --rx-end.
    """
    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')
        return

    def filter_packets(self, options):
        """
        Returns a list containing all of the packets that match options.
        In addition, all returned packets will have valid 'xmit' and 'gro'
        fields, and the sending and receiving RPCs will exist.

        options:   A dictionary of option values (see class doc for list of
                   valid options); usually contains the command-line options.
        """
        global packets, rpcs

        result = []
        for pkt in packets.values():
            if not 'id' in pkt:
                print('No id in pkt: %s' % (pkt))
            tx_id = pkt['id']
            rx_id = tx_id ^ 1
            if not 'gro' in pkt:
                continue
            if not 'xmit' in pkt:
                continue
            if (not rx_id in rpcs) or (not rx_id in rpcs):
                continue
            if (options.tx_start != None) and (pkt['xmit'] < options.tx_start):
                continue
            if (options.tx_end != None) and (pkt['xmit'] >= options.tx_end):
                continue
            if (options.rx_start != None) and (pkt['gro'] < options.rx_start):
                continue
            if (options.rx_end != None) and (pkt['gro'] >= options.rx_end):
                continue
            if (options.tx_node != None) and (options.tx_node
                    != rpcs[tx_id]['node']):
                continue
            if (options.rx_node != None) and (options.rx_node
                    != rpcs[rx_id]['node']):
                continue
            if (options.tx_core != None) and (options.tx_core != pkt['tx_core']):
                continue
            if (options.rx_core != None) and (options.rx_core != pkt['gro_core']):
                continue
            result.append(pkt)
        return result

    def output(self):
        global options

        pkts = self.filter_packets(options)

        print('\n-------------------')
        print('Analyzer: filter')
        print('-------------------\n')
        if not pkts:
            print('No packets matched filters')
            return
        tx_filter = 'xmit:'
        if options.tx_node != None:
            tx_filter += ' node %s' % (options.tx_node)
        if options.tx_core != None:
            tx_filter += ' core %d' % (options.tx_core)
        if options.tx_start != None:
            if options.tx_end != None:
                tx_filter += ' time %9.3f-%9.3f' % (
                        options.tx_start, options.tx_end)
            else:
                tx_filter += ' time >= %9.3f' % (options.tx_start)
        elif options.tx_end != None:
            tx_filter += ' time < %9.3f' % (options.tx_end)

        rx_filter = 'gro:'
        if options.rx_node != None:
            rx_filter += ' node %s' % (options.rx_node)
        if options.rx_core != None:
            rx_filter += ' core %d' % (options.rx_core)
        if options.rx_start != None:
            if options.rx_end != None:
                rx_filter += ' time %9.3f-%9.3f' % (
                        options.rx_start, options.rx_end)
            else:
                rx_filter += ' time >= %9.3f' % (options.rx_start)
        elif options.rx_end != None:
            rx_filter += ' time < %9.3f' % (options.rx_end)

        print('Packets below matched these filters:')
        if len(tx_filter) > 5:
            print(tx_filter)
        if len(rx_filter) > 4:
            print(rx_filter)
        print('Packet information:')
        print('TxTime:  Time when ip*xmit was invoked for packet')
        print('TxNode:  Node that transmitted packet')
        print('TxCore:  Core on which ip*xmit was invoked for packet')
        print('RxTime:  Time when homa_gro_receive was invoked for packet')
        print('Delay:   RxTime - TxTime')
        print('RxNode:  Node that received packet')
        print('RxCore:  Core where home_gro_receive was invoked for packet')
        print('Prio:    Priority of packet')
        print('Len:     Bytes of message data in packet')
        print('Tx_Id:   RPC ID on sender')
        print('Offset:  Offset of first byte of data in packet')
        print('')

        print('TxTime        TxNode TxCore  RxTime  Delay     RxNode RxCore Prio   Len      Tx_Id Offset')
        print('-----------------------------------------------------------------------------------------')
        pkts.sort(key=lambda d : d['xmit'] if 'xmit' in d else 0)
        for pkt in pkts:
            tx_id = pkt['id']
            rx_id = tx_id ^ 1
            print('%9.3f %10s %s %9.3f %6.1f %10s   %3d   %2d %6d %10d %7d' % (
                    pkt['xmit'], rpcs[tx_id]['node'],
                    print_field_if(pkt, 'tx_core', '%4d'),
                    pkt['gro'], pkt['gro'] - pkt['xmit'],
                    rpcs[rx_id]['node'], pkt['gro_core'], pkt['priority'],
                    get_recv_length(pkt['offset'], pkt['msg_length']),
                    tx_id, pkt['offset']))

#------------------------------------------------
# Analyzer: grantablelock
#------------------------------------------------
class AnalyzeGrantablelock:
    """
    Analyzes contention for the grantable lock, which controls centrally
    managed data about grantable RPCs.
    """

    def __init__(self, dispatcher):

        # Node name -> dictionary with data about that node:
        # last_block:  core -> last time that core blocked on the lock
        # block_time:  core -> total time that core was blocked on the lock
        # total_hold:  total time this core spent holding the lock
        # hold_count:  number of distinct intervals with this core held the lock
        # max_hold:    max amount of time lock was held before releasing
        # max_time:    time when lock was released after max_hold
        #
        self.nodes = {}

        # One record for each interval where a core blocked for the grantable
        # lock: <time, duration, node, core> where time is when the lock was
        # finally acquired, duration is how long the core had to wait, and
        # node and core indicate where the block occurred.
        self.block_intervals = []

        # One record for each interval where it can be determined that the
        # lock was held by one core: <time, duration, node, core>, where
        # time is when the lock was acquired, duration is the elapsed time
        # until the next core got the lock, and core is the core that
        # acquired the lock.
        self.hold_times = []

        # Number of cores currently blocked on the lock.
        self.blocked_cores = 0

        # The last time that a core unblocked after waiting for the lock
        # when at least one other core was waiting for the lock (used to
        # compute hold_times).
        self.last_unblock = None

        # The core where last_unblock occurred.
        self.last_core = None

    def init_trace(self, trace):
        self.node = {
            'last_block': {},
            'block_times': defaultdict(lambda: 0),
            'block_time': 0,
            'total_hold': 0,
            'hold_count': 0,
            'max_hold': 0,
            'max_time': 0}
        self.nodes[trace['node']] = self.node
        self.blocked_cores = 0
        self.last_unblock = None

    def tt_lock_wait(self, trace, time, core, event, lock_name):
        if lock_name != 'grantable':
            return
        if event == 'beginning':
            # Core blocked on lock
            self.node['last_block'][core] = time
            self.blocked_cores += 1
        else:
            # Blocked core acquired lock
            if core in self.node['last_block']:
                duration = time - self.node['last_block'][core]
                self.node['block_times'][core] += duration
                self.block_intervals.append([time, duration, trace['node'],
                        core])
                if self.last_unblock != None:
                    hold = time - self.last_unblock
                    self.hold_times.append([self.last_unblock, hold,
                            trace['node'], self.last_core])
                    self.node['total_hold'] += hold
                    self.node['hold_count'] += 1
                    if hold > self.node['max_hold']:
                        self.node['max_hold'] = hold
                        self.node['max_time'] = time
                self.blocked_cores -= 1
                if self.blocked_cores > 0:
                    self.last_unblock = time
                    self.last_core = core
                else:
                    self.last_unblock = None

    def output(self):
        global traces

        print('\n-----------------------')
        print('Analyzer: grantablelock')
        print('-----------------------\n')

        print('Per-node statistics on usage of the grantable lock:')
        print('Node:     Name of node')
        print('Blocked:  Fraction of core(s) wasted while blocked on the lock '
                '(1.0 means')
        print('          that on average, one core was blocked on the lock)')
        print('MaxCore:  The core that spent the largest fraction of its time '
                'blocked on')
        print('          the grantable lock')
        print('MaxBlk:   Fraction of time that MaxCore was blocked on the lock')
        print('HoldFrac: Fraction of time this node held the lock (note: '
                'hold times ')
        print('          can be computed only when there are 2 or more '
                  'waiting cores,')
        print('          so this is an underestimate)')
        print('AvgHold:  Average time that lock was held before releasing')
        print('MaxHold:  Largest time that lock was held before releasing')
        print('MaxTime:  Time when MaxHold ended')
        print('')

        # <node, total_block, max_block, max_core>
        data = []
        for name, node in self.nodes.items():
            total_block = 0
            max_block_time = -1
            max_block_core = -1
            for core in sorted(node['block_times']):
                t = node['block_times'][core]
                total_block += t
                if t > max_block_time:
                    max_block_time = t
                    max_block_core = core
            data.append([name, total_block, max_block_time, max_block_core])

        print('Node     Blocked MaxCore MaxBlk HoldFrac AvgHold  MaxHold    MaxTime')
        print('--------------------------------------------------------------------')
        for name, total_block, max_block, max_block_core in sorted(
                data, key=lambda t : t[1], reverse = True):
            elapsed = traces[name]['elapsed_time']
            node = self.nodes[name]
            if node['hold_count'] > 0:
                hold_info = '    %4.2f  %6.2f  %7.2f %10.3f' % (
                        node['total_hold']/elapsed,
                        node['total_hold']/node['hold_count'],
                        node['max_hold'], node['max_time'])
            else:
                hold_info = '    0.00     N/A     N/A        N/A'
            print('%-10s %5.2f     C%02d %6.3f %s' % (name, total_block/elapsed,
                    max_block_core, max_block/elapsed, hold_info))

        print('\nLongest times a core had to wait for the grantable lock:')
        print('  EndTime BlockTime       Node Core')
        self.block_intervals.sort(key=lambda t : t[1], reverse=True)
        for i in range(len(self.block_intervals)):
            if i >= 10:
                break
            time, duration, node, core = self.block_intervals[i]
            print('%9.3f   %7.1f %10s %4d' % (time, duration, node, core))

        print('\nLongest periods that one core held the grantable lock:')
        print('StartTime  HoldTime       Node Core')
        self.hold_times.sort(key=lambda t : t[1], reverse=True)
        for i in range(len(self.hold_times)):
            if i >= 10:
                break
            time, duration, node, core = self.hold_times[i]
            print('%9.3f   %7.1f %10s %4d' % (time, duration, node, core))

#------------------------------------------------
# Analyzer: grants
#------------------------------------------------
class AnalyzeGrants:
    """
    Generates statistics about the granting mechanism, such as the number of
    grants outstanding for incoming messages and the number of granted bytes
    available for outgoing messages. If --data is specified, then two files
    are created for each node in the data directory, with names
    "grants_rx_<node>" and "grants_tx_<node>". These files contain information
    about all incoming/outgoing RPCs with outstanding/available grants in each
    time interval. In addition, statistics are generated about the time spent
    in homa_grant_check_rpc and homa_grant_recalc.
    """
    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzeIntervals')

        # Node name -> total time spent in homa_grant_check_rpc on that node,
        # including time in homa_grant_recalc and time spent sending grants.
        self.node_check_time = defaultdict(lambda : 0)

        # Node name -> total time spent in homa_grant_recalc on that node,
        # not including sending grants.
        self.node_recalc_time = defaultdict(lambda : 0)

        # Node name -> total time spent sending grants during homa_grant_check_rpc.
        self.node_grant_send_time = defaultdict(lambda : 0)

        # Node name -> number of calls to homa_grant_recalc
        self.node_recalcs = defaultdict(lambda : 0)

        # Node name -> number of calls to homa_grant_check_rpc
        self.node_checks = defaultdict(lambda : 0)

        # Node name -> count of grants sent in calls to homa_grant_check_rpc
        self.node_grants_sent = defaultdict(lambda : 0)

    def init_trace(self, trace):
        # Core -> start time of active call to homa_grant_check_rpc (if any)
        self.core_check_start = {}

        # Core -> start time of active call to recalc start (if any)
        self.core_recalc_start = {}

        # Core -> time of first grant sent by current call to
        # homa_grant_check_rpc (only valid if homa_grant_check_rpc in progress)
        self.core_first_grant_send = {}

    def tt_grant_check_start(self, trace, t, core, id):
        self.node_checks[trace['node']] += 1
        self.core_check_start[core] = t

    def tt_grant_recalc_start(self, trace, t, core):
        node = trace['node']
        self.node_recalcs[node] += 1
        self.core_recalc_start[core] = t
        if core in self.core_first_grant_send:
            self.node_grant_send_time[node] += t - self.core_first_grant_send[core]
            del self.core_first_grant_send[core]

    def tt_send_grant(self, trace, t, core, id, offset, priority, increment):
        if not core in self.core_check_start:
            return
        self.node_grants_sent[trace['node']] += 1
        if not core in self.core_first_grant_send:
            self.core_first_grant_send[core] = t

    def tt_grant_check_done(self, trace, t, core, id):
        node = trace['node']
        if core in self.core_first_grant_send:
            grant = self.core_first_grant_send[core]
            self.node_grant_send_time[node] += (t - grant)
            del self.core_first_grant_send[core]
        else:
            grant = -1e20
        if core in self.core_recalc_start:
            recalc_start = self.core_recalc_start[core]
            end = t
            if grant > recalc_start:
                end = grant
            self.node_recalc_time[node] += end - self.core_recalc_start[core]
            del self.core_recalc_start[core]
        if core in self.core_check_start:
            self.node_check_time[node] += t - self.core_check_start[core]
            del self.core_check_start[core]

    def print_grant_check_stats(self):
        print('\nStatistics about the functions homa_grant_check_rpc and '
                'homa_grant_recalc:')
        print('Node:    Name of node')
        print('Checks:  Rate of calling homa_grant_check_rpc (k/sec)')
        print('CUsec:   Average execution time in homa_grant_check_rpc, '
                'not including')
        print('         time in homa_grant_recalc or sending grants')
        print('CCores:  Average active cores in homa_grant_check_rpc, '
                'not including')
        print('         time in homa_grant_recalc or sending grants')
        print('RFrac:   Fraction of calls to homa_grant_check_rpc that '
                '   invoked homa_grant_recalc')
        print('RUsec:   Average execution time in homa_grant_recalc, '
                'not including time')
        print('         sending grants')
        print('RCores:  Average active cores in homa_grant_recalc, '
                'not including time')
        print('         sending grants')
        print('GPer     Average grants sent per call to homa_grant_check_rpc')
        print('GUsec    Average time to send a grant')
        print('GCores   Average cores actively sending grants from within '
                'homa_grant_check_rpc')

        print('')
        print('Node      Checks CUsec CCores RFrac RUsec RCores  '
                'GPer GUSec GCores')
        print('--------------------------------------------------'
                '-----------------')
        for node in get_sorted_nodes():
            checks = self.node_checks[node]
            recalcs = self.node_recalcs[node]
            grants = self.node_grants_sent[node]
            recalc_time = self.node_recalc_time[node]
            grant_time = self.node_grant_send_time[node]
            check_time = self.node_check_time[node] - recalc_time - grant_time
            elapsed = traces[node]['elapsed_time']
            print('%-10s %5.1f %5.2f  %5.2f ' % (node, 1000*checks/elapsed,
                    check_time/checks if checks else 0, check_time/elapsed),
                    end='')
            print('%5.2f %5.2f  %5.2f ' % (recalcs/checks if checks else 0,
                    recalc_time/recalcs if recalcs else 0,
                    recalc_time/elapsed), end='')
            print('%5.2f %5.2f  %5.2f' % (grants/checks if checks else 0,
                    grant_time/grants if grants else 0, grant_time/elapsed))

    def get_events(self):
        """
        Returns a list of events of interest for this analyzer. Elements
        in the list have one of the following forms:
        <time, "txdata", node, id, offset, length>
            Time is when data packet was passed to ip_queue_xmit on node.
            Id is an RPC id, offset is the offset of first data byte in packet
            and length is the number of data bytes in the packet.
        <time, "rxdata", node, id, offset, length>
            Similar to "txdata" except describes when homa_softirq processed
            the packet on the receiving node.
        <time, "txgrant", node, id, offset>
            Time is when a grant packet was created (in homa_create_grants) on
            node, id identifies the RPC for the grant, and offset is the byte
            just after the last one now granted for the RPC. Used for
            unscheduled bytes as well as actual grants.
        <time, "rxgrant", node, id, offset>
            Similar to "txgrant" except the time is when homa_grant_pkt
            processed the packet on the receiving node.
        The return value will be sorted by time.
        """
        global rpcs

        events = []
        for id, rpc in rpcs.items():
            node = rpc['node']

            if 'sendmsg' in rpc:
                if id^1 in rpcs:
                    other = rpcs[id^1]
                else:
                    other = {}
                if 'unsched' in other:
                    unsched = other['unsched']
                else:
                    unsched = max_unsched;
                    if 'out_length' in rpc:
                        if rpc['out_length'] < max_unsched:
                            unsched = rpc['out_length']
                events.append([rpc['sendmsg'], 'rxgrant', node, id, unsched])

            if 'send_data' in rpc:
                for time, offset, length in rpc['send_data']:
                    events.append([time, 'txdata', node, id, offset, length])
            if rpc['softirq_data']:
                for time, offset in rpc['softirq_data']:
                    events.append([time, 'rxdata', node, id, offset,
                            get_recv_length(offset, rpc['in_length'])])
            if 'send_grant' in rpc:
                for time, offset, priority, increment in rpc['send_grant']:
                    events.append([time, 'txgrant', node, id, offset])
            if 'softirq_grant' in rpc:
                for time, offset in rpc['softirq_grant']:
                    events.append([time, 'rxgrant', node, id, offset])
        return sorted(events)

    class RpcDict(dict):
        """
        id -> dict for each RPC; dict contains any or all of:
        rx_data_offset:  Offset just after last byte received
        tx_data_offset:  Offset just after last byte sent; -1 means no data
                         was ever sent for this message, so we ignore it
                         for tx stats
        rx_grant_offset: Offset just after last incoming byte granted
        tx_grant_offset: Offset just after last outgoing byte granted
        rx_length:       Length of incoming message for this RPC, or -1 if
                         not known
        tx_length:       Length of outgoing message for this RPC, or -1 if
                         not known
        """
        def __missing__(self, key):
            global rpcs
            self[key] = {'rx_data_offset': 0, 'tx_data_offset': -1,
                    'rx_grant_offset': 0, 'tx_grant_offset': 0}
            record = self[key]
            rpc = rpcs[key]
            if rpc['in_length'] != None:
                record['rx_length'] = rpc['in_length']
            else:
                record['rx_length'] = -1
            if 'out_length' in rpc:
                record['tx_length'] = rpc['out_length']
            else:
                record['tx_length'] = -1
            if rpc['send_data']:
                record['tx_data_offset'] = rpc['send_data'][0][1]
            return record

    class NodeDict(dict):
        """
        node -> dict for each node; dict contains:
        name:           Name of the node
        rx_bytes:       Total bytes across all incoming messages that
                        have been granted but not yet received
        tx_bytes:       Total bytes across all outgoing messages that
                        have been granted but not yet sent
        rx_rpcs:        Id -> True for all incoming messages with granted
                        bytes that haven't yet been received
        tx_rpcs:        Id -> True for all outgoing messages with granted
                        bytes that haven't yet been sent
        *_integral:     For each of the above, sum of value*dt
        tx_times:       Array: element n is total time when tx_msgs was n
        rx_times:       Array: element n is total time when rx_msgs was n
        prev_time:      The last time any of the stats above were changed
        rx_data:        Accumulates detailed grant info for incoming messages
                        when --data is specified; one line per interval
        tx_data:        Accumulates detailed grant info for outgoing messages
                        when --data is specified; one line per interval
        """
        def __missing__(self, key):
            global traces
            self[key] = {'name': key, 'rx_bytes': 0, 'tx_bytes': 0,
                    'rx_rpcs': {}, 'tx_rpcs': {},
                    'rx_bytes_integral': 0, 'tx_bytes_integral': 0,
                    'rx_msgs_integral': 0, 'tx_msgs_integral': 0,
                    'tx_times': [0, 0, 0, 0], 'rx_times': [0, 0, 0, 0],
                    'prev_time': traces[key]['first_time'],
                    'rx_data': '', 'tx_data': ''}
            return self[key]

    def check_node(self, node, local_rpcs):
        """
        Check consistency of node with current state of RPCs.

        node:        Node to check (element of NodeDict).
        local_rpcs:  RpcDict containing information about RPCs.
        """

        global rpcs
        rx_bytes = 0
        rx_msgs = 0
        tx_bytes = 0
        tx_msgs = 0
        node_name = node['name']
        for id, rpc in local_rpcs.items():
            if rpcs[id]['node'] != node_name:
                continue
            delta = rpc['rx_grant_offset'] - rpc['rx_data_offset']
            if delta > 0:
                rx_msgs += 1
                rx_bytes += delta
            delta = rpc['tx_grant_offset'] - rpc['tx_data_offset']
            if delta > 0:
                tx_msgs += 1
                tx_bytes += delta
        if rx_msgs != len(node['rx_rpcs']):
            print('Error for RPC %d rx_msgs" expected %d, got %d' %
                    (id, rx_msgs, len(node['rx_rpcs'])))
        if rx_bytes != node['rx_bytes']:
            print('Error for RPC %d rx_bytes" expected %d, got %d' %
                    (id, rx_bytes, node['rx_bytes']))
        if tx_msgs != len(node['tx_rpcs']):
            print('Error for RPC %d tx_msgs" expected %d, got %d' %
                    (id, tx_msgs, len(node['tx_rpcs'])))
        if tx_bytes != node['tx_bytes']:
            print('Error for RPC %d tx_bytes" expected %d, got %d' %
                    (id, tx_bytes, node['tx_bytes']))

    def rx_info(self, node, local_rpcs):
        """
        Return a line of text describing the current state of grants for
        incoming messages for a node.

        node:        Node of interest (element of NodeDict).
        local_rpcs:  RpcDict containing information about RPCs.
        """

        records = []
        for id in node['rx_rpcs']:
            rpc = local_rpcs[id]
            length = rpc['rx_length']
            data_offset = rpc['rx_data_offset']
            grant_offset = rpc['rx_grant_offset']
            outstanding = grant_offset - data_offset
            if outstanding < 0:
                outstanding = 0
            if length >= 0:
                records.append([length - data_offset, id, outstanding])
            else:
                records.append([1e20, id, outstanding])
        records.sort(reverse=True)
        result = ''
        for remaining, id, outstanding in records:
            if remaining == 1e20:
                result += '%12d     ?? %6d' % (id, outstanding)
            else:
                result += '%12d %6d %6d' % (id, remaining, outstanding)
        return result

    def tx_info(self, node, local_rpcs):
        """
        Return a line of text describing the current state of grants for
        outgoing messages for a node.

        node:        Node of interest (element of NodeDict).
        local_rpcs:  RpcDict containing information about RPCs.
        """

        records = []
        for id in node['tx_rpcs']:
            rpc = local_rpcs[id]
            length = rpc['tx_length']
            data_offset = rpc['tx_data_offset']
            grant_offset = rpc['tx_grant_offset']
            available = grant_offset - data_offset
            if available < 0:
                available = 0
            if length >= 0:
                records.append([length - data_offset, id, available])
            else:
                records.append([1e20, id, available])
        records.sort(reverse=True)
        result = ''
        for remaining, id, available in records:
            if remaining == 1e20:
                result += '%12d    ?? %6d' % (id, available)
            else:
                result += '%12d %6d %6d' % (id, remaining, available)
        return result

    def analyze(self):
        global options, rpcs, intervals

        events = self.get_events()
        interval_end = get_first_interval_end()

        self.local_rpcs = self.RpcDict()
        self.local_nodes = self.NodeDict()

        count = 0
        for event in events:
            t, op, node_name, id, offset = event[0:5]
            rpc = self.local_rpcs[id]
            node = self.local_nodes[node_name]

            while (t > interval_end) and options.data:
                for name2, node2 in self.local_nodes.items():
                    interval = get_interval(name2, interval_end)
                    if interval == None:
                        continue
                    interval['rx_grants'] = len(node2['rx_rpcs'])
                    interval['rx_grant_bytes'] = node2['rx_bytes']
                    interval['rx_grant_info'] = self.rx_info(node2, self.local_rpcs)
                    interval['tx_grant_info'] = self.tx_info(node2, self.local_rpcs)
                interval_end += options.interval

            # Update integrals.
            delta = t - node['prev_time']
            rx_msgs = len(node['rx_rpcs'])
            tx_msgs = len(node['tx_rpcs'])
            node['rx_bytes_integral'] += node['rx_bytes'] * delta
            node['tx_bytes_integral'] += node['tx_bytes'] * delta
            node['rx_msgs_integral'] += rx_msgs * delta
            node['tx_msgs_integral'] += tx_msgs * delta
            if rx_msgs < 4:
                node['rx_times'][rx_msgs] += delta
            if tx_msgs < 4:
                node['tx_times'][tx_msgs] += delta
            node['prev_time'] = t

            # Update state
            if op == 'rxdata':
                offset += event[5]
                old_offset = rpc['rx_data_offset']
                if offset <= old_offset:
                    continue
                rpc['rx_data_offset'] = offset
                grant = rpc['rx_grant_offset']
                if old_offset < grant:
                    if offset >= grant:
                        node['rx_bytes'] -= grant - old_offset
                        del node['rx_rpcs'][id]
                    else:
                        node['rx_bytes'] -= offset - old_offset
                if 0 and node_name == 'node1':
                    print('%9.3f id %12d old_offset %7d new_offset %7d grant %7d, '
                            'rx_bytes %7d, rx_msgs %7d'
                            % (t, id, old_offset, offset, grant,
                            node['rx_bytes'], len(node['rx_rpcs'])))

            if op == 'txdata':
                offset += event[5]
                old_offset = rpc['tx_data_offset']
                if (offset < old_offset) or (old_offset < 0):
                    continue
                grant = rpc['tx_grant_offset']
                rpc['tx_data_offset'] = offset
                if old_offset < grant:
                    if offset >= grant:
                        node['tx_bytes'] -= grant - old_offset
                        del node['tx_rpcs'][id]
                    else:
                        node['tx_bytes'] -= offset - old_offset
                if 0 and node_name == 'node1':
                    print('%9.3f: data %d, state %s' % (t, offset, rpc))

            if op == 'rxgrant':
                old_grant = rpc['tx_grant_offset']
                if (offset < old_grant) or (rpc['tx_data_offset'] < 0):
                    continue
                data = rpc['tx_data_offset']
                rpc['tx_grant_offset'] = offset
                if offset > data:
                    if old_grant > data:
                        node['tx_bytes'] += offset - old_grant
                    else:
                        node['tx_bytes'] += offset - data
                        node['tx_rpcs'][id] = True

            if op == 'txgrant':
                old_grant = rpc['rx_grant_offset']
                data = rpc['rx_data_offset']
                if offset < old_grant:
                    continue
                rpc['rx_grant_offset'] = offset
                if offset > data:
                    if old_grant > data:
                        node['rx_bytes'] += offset - old_grant
                    else:
                        node['rx_bytes'] += offset - data
                        node['rx_rpcs'][id] = True
                if 0 and node_name == 'node1':
                    print('%9.3f: grant %d, state %s' % (t, offset, rpc))

            if 0 and node_name == 'node1':
                count += 1
                if (count % 10) == 0:
                    self.check_node(node, self.local_rpcs)

    def output(self):
        print('\n-------------------')
        print('Analyzer: grants')
        print('-------------------\n')

        print('Grant statistics:')
        print('Node:     Name of node')
        print('InMsgs:   Average number of incoming messages with outstanding grants')
        print('InN:      Fraction of time when N incoming messages had outstanding grants')
        print('InKB:     Average KB of outstanding grants across all incoming messages')
        print('OutMsgs:  Average number of outgoing messages with available grants')
        print('OutN:     Fraction of time when N outgoing messages had available grants')
        print('OutKB:    Average KB of available grants across all outgoing messages')
        print('')
        print('Node       InMsgs  In0  In1  In2  In3   InKB OutMsgs Out0 Out1 Out2 Out3  OutKB')
        print('-------------------------------------------------------------------------------')
        total_in_msgs = 0
        total_in_bytes = 0
        total_out_msgs = 0
        total_out_bytes = 0
        for n in get_sorted_nodes():
            node = self.local_nodes[n]
            total_time = node['prev_time'] - traces[n]['first_time']
            total_in_msgs += node['rx_msgs_integral']/total_time
            total_in_bytes += node['rx_bytes_integral']/total_time
            total_out_msgs += node['tx_msgs_integral']/total_time
            total_out_bytes += node['tx_bytes_integral']/total_time
            if total_time == 0:
                print('%-10s  No data available' % (node))
            print('%-10s %6.1f %4.2f %4.2f %4.2f %4.2f %6.1f  %6.1f %4.2f '
                    '%4.2f %4.2f %4.2f %6.1f' % (n,
                    node['rx_msgs_integral']/total_time,
                    node['rx_times'][0]/total_time,
                    node['rx_times'][1]/total_time,
                    node['rx_times'][2]/total_time,
                    node['rx_times'][3]/total_time,
                    node['rx_bytes_integral']*1e-3/total_time,
                    node['tx_msgs_integral']/total_time,
                    node['tx_times'][0]/total_time,
                    node['tx_times'][1]/total_time,
                    node['tx_times'][2]/total_time,
                    node['tx_times'][3]/total_time,
                    node['tx_bytes_integral']*1e-3/total_time))
        print('Average    %6.1f                     %6.1f  %6.1f     '
                '                %6.1f' % (
                total_in_msgs/len(self.local_nodes),
                total_in_bytes/len(self.local_nodes)*1e-3,
                total_out_msgs/len(self.local_nodes),
                total_out_bytes/len(self.local_nodes)*1e-3))

        # Create data files.
        if options.data:
            for name, node in self.local_nodes.items():
                f = open('%s/grants_rx_%s.dat' % (options.data, name), 'w')
                f.write('# Node: %s\n' % (name))
                f.write('# Generated at %s.\n' %
                        (time.strftime('%I:%M %p on %m/%d/%Y')))
                f.write('# Incoming messages with outstanding grants, as a '
                        'function of time.\n')
                f.write('# Time:    End of the time interval\n')
                f.write('# IdN:     Rpc identifier\n')
                f.write('# RemN:    Number of bytes that have not yet been '
                        'received for\n')
                f.write('#          the message\n')
                f.write('# GrantN:  Number of bytes that have been granted '
                        'but data has\n')
                f.write('#          not yet arrived\n')
                f.write('\n')
                f.write('   Time          Id1   Rem1 Grant1         '
                        'Id2   Rem2 Grant2         Id3   Rem3 Grant3\n')
                for interval in intervals[name]:
                    if not 'rx_grant_info' in interval:
                        continue
                    f.write('%7.1f %s\n' % (interval['time'],
                            interval['rx_grant_info']))
                f.close()

                f = open('%s/grants_tx_%s.dat' % (options.data, name), 'w')
                f.write('# Node: %s\n' % (name))
                f.write('# Generated at %s.\n' %
                        (time.strftime('%I:%M %p on %m/%d/%Y')))
                f.write('# Outgoing messages with available grants, as a '
                        'function of time.\n')
                f.write('# Time:    End of the time interval\n')
                f.write('# IdN:     Rpc identifier\n')
                f.write('# RemN:    Number of bytes that have not yet been '
                        'transmitted for\n')
                f.write('#          the message\n')
                f.write('# GrantN:  Number of bytes that have been granted '
                        'but data has\n')
                f.write('#          not yet been transmitted\n')
                f.write('\n')
                f.write('   Time          Id1   Rem1 Grant1         '
                        'Id2   Rem2 Grant2         Id3   Rem3 Grant3\n')
                for interval in intervals[name]:
                    if not 'tx_grant_info' in interval:
                        continue
                    f.write('%7.1f %s\n' % (interval['time'],
                            interval['tx_grant_info']))
                f.close()

        # Print stats related to homa_grant_check_rpc.
        self.print_grant_check_stats()

#------------------------------------------------
# Analyzer: handoffs
#------------------------------------------------
class AnalyzeHandoffs:
    """
    Analyzes handoff delays for incoming messages (time from when
    homa_rpc_handoff was called until homa_wait_for_message received
    the message).
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        return

    def output(self):
        global rpcs

        # node name -> list of delays in cases where there was a thread
        # waiting for the message; separate info for requests and responses
        node_req_handoffs = defaultdict(list)
        node_resp_handoffs = defaultdict(list)

        # node name -> list of delays where messages had to be queued;
        # separate info for requests and responses
        node_req_queued = defaultdict(list)
        node_resp_queued = defaultdict(list)

        for id, rpc in rpcs.items():
            if not 'found' in rpc:
                continue
            if 'handoff' in rpc:
                delay = rpc['found'] - rpc['handoff']
                if id & 1:
                    node_req_handoffs[rpc['node']].append(delay)
                else:
                    node_resp_handoffs[rpc['node']].append(delay)
            elif 'queued' in rpc:
                delay = rpc['found'] - rpc['queued']
                if id & 1:
                    node_req_queued[rpc['node']].append(delay)
                else:
                    node_resp_queued[rpc['node']].append(delay)

        print('\n------------------')
        print('Analyzer: handoffs')
        print('------------------')
        print('')
        print('Delays in handing off RPCs to an application thread (elapsed ')
        print('time from when homa_rpc_handoff was called at SoftIRQ level ')
        print('until homa_wait_for_message received the RPC in the application):')
        print('Node:         Name of node')
        print('FastFrac:     Fraction of messages that were handed directly to')
        print('              a waiting thread (no queueing)')
        print('FAvg:         Average delay for fast handoffs')
        print('FP50:         Median delay for fast handoffs')
        print('FP90:         90th percentile delay for fast handoffs')
        print('FP99:         99th percentile delay for fast handoffs')
        print('QAvg:         Average delay for handoffs where the message had')
        print('              to be queued (no waiting thread)')
        print('QP50:         Median delay for queued handoffs')
        print('QP90:         90th percentile delay for queued handoffs')
        print('QP99:         99th percentile delay for queued handoffs')
        print('')

        for i in [0, 1]:
            if i == 0:
                print("\nRequest messages:")
            else:
                print("\nResponse messages:")
            print('Node      FastFrac  Favg  FP50   FP90   FP99  QAvg  '
                    'QP50   QP90   QP99')
            print('----------------------------------------------------'
                    '------------------')

            for node in get_sorted_nodes():
                if i == 0:
                    handoffs = sorted(node_req_handoffs[node])
                    queued = sorted(node_req_queued[node])
                else:
                    handoffs = sorted(node_resp_handoffs[node])
                    queued = sorted(node_resp_queued[node])

                print('%-10s   %5.3f' % (node,
                        len(handoffs)/(len(handoffs) + len(queued))),
                        end='')
                if handoffs:
                    print(' %5.1f %5.1f %6.1f %6.1f' % (
                            sum(handoffs)/len(handoffs),
                            handoffs[(50*len(handoffs))//100],
                            handoffs[(90*len(handoffs))//100],
                            handoffs[(99*len(handoffs))//100]), end='')
                else:
                    print(' '*24, end='')
                if queued:
                    print(' %5.1f %5.1f %6.1f %6.1f' % (
                            sum(queued)/len(queued),
                            queued[(50*len(queued))//100],
                            queued[(90*len(queued))//100],
                            queued[(99*len(queued))//100]))
                else:
                    print('')

#------------------------------------------------
# Analyzer: incoming
#------------------------------------------------
class AnalyzeIncoming:
    """
    Generates detailed timelines of rates of incoming data and packets for
    each core of each node. Use the --data option to specify a directory for
    data files.
    """
    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')
        return

    def write_node_data(self, node, pkts, max):
        """
        Write a data file describing incoming traffic to a given node.

        node:   Name of the node
        pkts:   List of <time, length, core, priority> tuples describing
                packets on that node.
        max:    Dictionary with values that accumulate information about the
                highest throughput seen.
        """
        global options

        interval = options.interval

        # Figure out which cores received packets on this node.
        cores = {}
        for pkt in pkts:
            cores[pkt[2]] = 1
        core_ids = sorted(cores.keys())

        pkts.sort(key=lambda t : t[0])
        start = pkts[0][0]
        interval_end = (start//interval) * interval
        if interval_end > start:
            interval_end -= interval

        core_bytes = {}
        core_pkts = {}
        min_prio = 100

        f = open('%s/incoming_%s.dat' % (options.data, node), 'w')
        f.write('# Node: %s\n' % (node))
        f.write('# Generated at %s.\n' %
                (time.strftime('%I:%M %p on %m/%d/%Y')))
        f.write('# Rate of arrival of incoming data and packets, broken down\n')
        f.write('# by core and time interval:\n')
        f.write('# Time:    End of the time interval\n')
        f.write('# GbpsN:   Data arrival rate on core N for the '
                'interval (Gbps)\n')
        f.write('# PktsN:   Total packets (grants and data) that arrived on '
                'core N in the interval\n')
        f.write('# Gbps:    Total arrival rate of data across all '
                'cores (Gbps)\n')
        f.write('# Pkts:    Total packet arrivals (grants and data) across all '
                'cores\n')
        f.write('# MinP:    Lowest priority level for any incoming packet\n')
        f.write('\nInterval')
        for c in core_ids:
            f.write(' %6s %6s' % ('Gps%d' % c, 'Pkts%d' % c))
        f.write('   Gbps   Pkts  MinP\n')

        for t, length, core, priority in pkts:
            while t >= interval_end:
                if interval_end > start:
                    f.write('%8.1f' % (interval_end))
                    total_gbps = 0
                    total_pkts = 0
                    for c in core_ids:
                        gbps = 8*core_bytes[c]/(interval*1e03)
                        f.write(' %6.1f %6d' % (gbps, core_pkts[c]))
                        total_gbps += gbps
                        total_pkts += core_pkts[c]
                        if core_pkts[c] > max['core_pkts']:
                            max['core_pkts'] = core_pkts[c]
                            max['core_pkts_time'] = interval_end
                            max['core_pkts_core'] = '%s, core %d' % (node, c)
                        if gbps > max['core_gbps']:
                            max['core_gbps'] = gbps
                            max['core_gbps_time'] = interval_end
                            max['core_gbps_core'] = '%s, core %d' % (node, c)
                    f.write('  %5.1f  %5d   %3d\n' % (total_gbps,
                            total_pkts, min_prio))
                    if total_pkts > max['node_pkts']:
                        max['node_pkts'] = total_pkts
                        max['node_pkts_time'] = interval_end
                        max['node_pkts_node'] = node
                    if total_gbps > max['node_gbps']:
                        max['node_gbps'] = total_gbps
                        max['node_gbps_time'] = interval_end
                        max['node_gbps_node'] = node
                for c in core_ids:
                    core_bytes[c] = 0
                    core_pkts[c] = 0
                    min_prio = 7
                interval_end += 20
            core_pkts[core] += 1
            core_bytes[core] += length
            if priority < min_prio:
                min_prio = priority
        f.close()

    def output(self):
        global packets, grants, options, rpcs

        # Node name -> list of packets for that node. Each packet is described
        # by a tuple <time, size, core> giving the arrival time and size of
        # the packet (size 0 means the packet was a grant) and the core where
        #it was received.
        nodes = defaultdict(list)

        if options.data == None:
            print('The incoming analyzer can\'t do anything without the '
                    '--data option')
            return

        skipped = 0
        total_pkts = 0
        for pkt in packets.values():
            if not 'gro' in pkt:
                continue
            length = get_recv_length(pkt['offset'], pkt['msg_length'])
            if not 'id' in pkt:
                print('Packet: %s' % (pkt))
            rpc = rpcs[pkt['id']^1]
            nodes[rpc['node']].append([pkt['gro'], length, rpc['gro_core'],
                    pkt['priority']])
            total_pkts += 1
        if skipped > 0:
            print('Incoming analyzer skipped %d packets out of %d (%.2f%%): '
                    'couldn\'t compute length' % (skipped, total_pkts,
                    100.0*(skipped//total_pkts)), file=sys.stderr)

        for grant in grants.values():
            if not 'gro' in grant:
                continue
            rpc = rpcs[grant['id']^1]
            nodes[rpc['node']].append([grant['gro'], 0, rpc['gro_core'], 7])

        print('\n-------------------')
        print('Analyzer: incoming')
        print('-------------------')
        if options.data == None:
            print('No --data option specified, data can\'t be written.')

        max = {
            'core_pkts': 0,    'core_pkts_time': 0,    'core_pkts_core': 0,
            'core_gbps': 0,    'core_gbps_time': 0,    'core_gbps_core': 0,
            'node_pkts': 0,    'node_pkts_time': 0,    'node_pkts_node': 0,
            'node_gbps': 0,    'node_gbps_time': 0,    'node_gbps_node': 0
        }
        for node, node_pkts in nodes.items():
              self.write_node_data(node, node_pkts, max)
        print('Maximum homa_gro_receive throughputs in a 20 usec interval:')
        print('    Packets per core: %4d (time %7.1f, %s)' % (max['core_pkts'],
                max['core_pkts_time'], max['core_pkts_core']))
        print('    Gbps per core:   %5.1f (time %7.1f, %s)' % (max['core_gbps'],
                max['core_gbps_time'], max['core_gbps_core']))
        print('    Packets per node: %4d (time %7.1f, %s)' % (max['node_pkts'],
                max['node_pkts_time'], max['node_pkts_node']))
        print('    Gbps per node:   %5.1f (time %7.1f, %s)' % (max['node_gbps'],
                max['node_gbps_time'], max['node_gbps_node']))

#------------------------------------------------
# Analyzer: intervals
#------------------------------------------------
class AnalyzeIntervals:
    """
    Populates the intervals global variable but doesn't actually print
    anything. Generates information that is used by other analyzers.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')
        self.tx_qid = None
        return

    def restrict_qid(self, qid):
        """
        Ignore all packets except thost that use the given transmit queue.
        """
        self.tx_qid = qid

    def init_intervals(self):
        global intervals, options

        # Initialize the intervals structure
        intervals = {}
        interval_length = options.interval
        for node in traces.keys():
            node_intervals = []
            t = get_first_interval_end(node)
            end = traces[node]['last_time'] + interval_length
            while t < end:
                node_intervals.append({
                    'time':             t,
                    'rpcs_live':        0,
                    'tx_starts':        0,
                    'tx_live_req':      0,
                    'tx_live_resp':     0,
                    'tx_pkts':          0,
                    'tx_bytes':         0,
                    'tx_nic_pkts':      0,
                    'tx_nic_bytes':     0,
                    'tx_in_nic':        0,
                    'tx_qdisc':         0,
                    'tx_q':             0,
                    'tx_free_bytes':    0,
                    'tx_max_free':      0,
                    'tx_min_free':      0,
                    'tx_max_gro_free':  None,
                    'tx_min_gro_free':  None,
                    'tx_grant_xmit':    0,
                    'tx_grant_gro':     0,
                    'tx_grant_avl':     0,
                    'tx_new_grants':    0,
                    'rx_starts':        0,
                    'rx_live':          0,
                    'rx_pkts':          0,
                    'rx_bytes':         0,
                    'rx_grantable':     0,
                    'rx_granted':       0,
                    'rx_data_qdisc':    0,
                    'rx_data_net':      0,
                    'rx_overdue':       0,
                    'rx_data_gro':      0,
                    'rx_new_grants':    0,
                    'rx_grants':        0,
                    'rx_grant_bytes':   0,
                    'rx_grant_info':    None,
                    'tx_grant_info':    None
                })
                t += interval_length
            intervals[node] = node_intervals

    def add_grant_info(self, rpc):
        """
        Analyzes incoming grants and outgoing packets for rpc and adds
        tx_grant_avl and rx_granted information to intervals.
        """

        # List of <time, event, offset> tuples, where event is one of:
        # grant_xmit:  time and offset describe a grant passed to ip*xmit
        #              by receiver
        # grant_softirq: time and offset describe a grant processed by
        #                SoftIRQ on sender.
        # data:          time and offset describe a data packet passed to
        #                ip*xmit by sender (offset is the byte just after the
        #                last one in the packet).
        global rpcs

        events = []
        id = rpc['id']
        if id^1 in rpcs:
            peer = rpcs[id^1]
            peer_node = peer['node']
            for t, offset, prio, increment in peer['send_grant']:
                events.append([t, 'grant_xmit', offset])

        grant_xmit_offset = 0
        grant_softirq_offset = 0
        data_offset = 1e20
        prev_time = 0
        node = rpc['node']
        for t, offset in rpc['softirq_grant']:
            events.append([t, 'grant_softirq', offset])
        for t, offset, length in rpc['send_data']:
            events.append([t, 'data', offset+length])
            if offset < data_offset:
                # Computes initial data_offset, for cases where initial
                # data packets aren't in the trace
                data_offset = offset
        if not events:
            return
        for t, event, offset in sorted(events, key=lambda t : t[0]):
            if grant_xmit_offset > data_offset:
                add_to_intervals(peer_node, prev_time, t, 'rx_granted',
                                    grant_xmit_offset - data_offset)
            if grant_softirq_offset > data_offset:
                add_to_intervals(node, prev_time, t, 'tx_grant_avl',
                                    grant_softirq_offset - data_offset)
            if event == 'grant_xmit':
                if offset > grant_xmit_offset:
                    grant_xmit_offset = offset
            elif event == 'grant_softirq':
                if offset > grant_softirq_offset:
                    grant_softirq_offset = offset
            elif event == 'data':
                if offset > data_offset:
                    data_offset = offset
            else:
                raise Exception('Unknown event type %s in add_grant_info' % (
                    event))
            prev_time = t

    def qlen(self, prev, elapsed):
        """
        Compute the new length of the NIC queue
        prev:     Previous length of the queue
        elapsed:  Amount of time that has passed with no new transmissions
                  added to the queue
        """
        global options
        xmit_bytes = ((elapsed) * (1000.0*options.gbps/8))
        if xmit_bytes < prev:
            new_length = prev - xmit_bytes
        else:
            new_length = 0
        return new_length

    def analyze(self):
        """
        Fill in fields of intervals related to incoming messages.
        """

        global rpcs, packets, grants, max_unsched, traces, options, intervals

        # Node name -> list of <time, length> pairs, where time gives the
        # time when a packet was transmitted by the NIC and length gives
        # the total length of the packet in bytes.
        node_xmits = defaultdict(list)

        # Total number of bytes a grant packet occupies on the wire, including
        # headers, inter-packet gap, etc.
        grant_bytes = 34 + 20 + 42

        # Total number of bytes a data packet occupies on the wire, including
        # everything *but* the actual data (headers, inter-packet gap, etc.)
        data_overhead_bytes = 60 + 20 + 42

        self.init_intervals()
        late_usecs = options.late

        # See if packets include NIC xmit times
        nic_data = False
        for pkt in packets.values():
            if ('xmit' in pkt) and ('gro' in pkt):
                if 'nic' in pkt:
                    nic_data = True
                break

        # Extract information from packets
        for pkt in packets.values():
            if (self.tx_qid != None) and ((not 'tx_qid' in pkt)
                    or (pkt['tx_qid'] != self.tx_qid)):
                continue
            tx_node = pkt['tx_node'] if 'tx_node' in pkt else None
            if not 'length' in pkt:
                print('Packet with no length: %s' % (pkt))
                continue
            length = pkt['length']
            txmit = pkt['xmit'] if 'xmit' in pkt else None
            tnic = pkt['nic'] if 'nic' in pkt else None
            tfree = pkt['free_tx_skb'] if 'free_tx_skb' in pkt else None
            tgro = pkt['gro'] if 'gro' in pkt else None

            # For tx statistics, process only the overall TSO frame, not the
            # individual segments
            if ('tso_length' in pkt):
                tso_length = pkt['tso_length']

                if txmit != None:
                    interval = get_interval(tx_node, txmit)
                    interval['tx_pkts'] += 1
                    interval['tx_bytes'] += tso_length
                    if 'nic' in pkt:
                        add_to_intervals(tx_node, txmit, pkt['nic'],
                            'tx_qdisc', tso_length)

                if 'nic' in pkt:
                    tnic = pkt['nic']
                    node_xmits[tx_node].append([pkt['nic'],
                            tso_length + data_overhead_bytes])
                    interval = get_interval(tx_node, tnic)
                    interval['tx_nic_pkts'] += 1
                    interval['tx_nic_bytes'] += tso_length
                elif txmit != None:
                    node_xmits[tx_node].append([txmit,
                            tso_length + data_overhead_bytes])

                if tfree != None:
                    interval = get_interval(tx_node, tfree)
                    interval['tx_free_bytes'] += tso_length
                    if 'nic' in pkt:
                        add_to_intervals(tx_node, tnic, tfree, 'tx_in_nic',
                                tso_length)
                        delay = tfree - tnic
                        if delay > interval['tx_max_free']:
                            interval['tx_max_free'] = delay
                        if (interval['tx_min_free'] == 0) or (delay <
                                interval['tx_min_free']):
                            interval['tx_min_free'] = delay
                    else:
                        start = traces[tx_node]['first_time']
                        add_to_intervals(tx_node, start, tfree, 'tx_in_nic',
                                tso_length)

            if not 'rx_node' in pkt:
                continue
            rx_node = pkt['rx_node']
            if tnic != None:
                if txmit != None:
                    add_to_intervals(rx_node, txmit, tnic, 'rx_data_qdisc', length)
                else:
                    add_to_intervals(rx_node, traces[tx_node]['first_time'],
                            tnic, 'rx_data_qdisc', length)
            elif not nic_data:
                tnic = txmit
            elif txmit != None:
                    add_to_intervals(rx_node, txmit, traces[tx_node]['last_time'],
                            'rx_data_qdisc', length)

            if tgro != None:
                interval = get_interval(rx_node, tgro)
                interval['rx_pkts'] += 1
                interval['rx_bytes'] += length
                if tnic != None:
                    add_to_intervals(rx_node, tnic, tgro, 'rx_data_net', length)
                    delay = tgro - tnic
                    if delay > late_usecs:
                        add_to_intervals(rx_node, tnic+late_usecs, tgro,
                                'rx_overdue', length)

            if (tgro != None) and (tfree != None):
                interval = get_interval(tx_node, tfree)
                if interval != None:
                    delay = tgro - tfree
                    if (interval['tx_max_gro_free'] == None) or (delay >
                            interval['tx_max_gro_free']):
                        interval['tx_max_gro_free'] = delay
                    if (interval['tx_min_gro_free'] == None) or (delay <
                            interval['tx_min_gro_free']):
                        interval['tx_min_gro_free'] = delay

            if 'softirq' in pkt:
                tsoftirq = pkt['softirq']
                if tgro != None:
                    add_to_intervals(rx_node, tgro, tsoftirq, 'rx_data_gro', length)
                else:
                    add_to_intervals(rx_node, traces[rx_node]['first_time'],
                            tsoftirq, 'rx_data_gro', length)
            elif tgro != None:
                add_to_intervals(rx_node, tgro, traces[rx_node]['last_time'],
                        'rx_data_gro', length)

        # Extract information from grants
        for grant in grants.values():
            offset = grant['offset']
            increment = grant['increment']
            rx_id = grant['id']^1
            if not rx_id in rpcs:
                continue

            if 'xmit' in grant:
                txmit = grant['xmit']
                get_interval(grant['tx_node'], txmit)['rx_new_grants'] += increment

            # rx_* refers to the RPC that received the grant and tx'ed data
            rx_rpc = rpcs[rx_id]
            rx_node = rx_rpc['node']
            if 'gro' in grant:
                tgro = grant['gro']
                if 'xmit' in grant:
                    add_to_intervals(rx_node, txmit, tgro, 'tx_grant_xmit',
                            increment)
            else:
                tgro = None

            if 'softirq' in grant:
                tsoftirq = grant['softirq']
                get_interval(rx_node, tsoftirq)['tx_new_grants'] += increment
                if tgro != None:
                    add_to_intervals(rx_node, tgro, tsoftirq, 'tx_grant_gro',
                            increment)
            elif tgro != None:
                add_to_intervals(rx_node, tgro, traces[rx_node]['last_time'],
                        'tx_grant_gro', increment)

            if 'nic' in grant:
                node_xmits[grant['tx_node']].append([grant['nic'], grant_bytes])
            elif 'xmit' in grant:
                node_xmits[grant['tx_node']].append([txmit, grant_bytes])

        # Extract information from RPCs
        for id, rpc in rpcs.items():
            node = rpc['node']
            if not id & 1:
                # rpcs_live
                start = None
                if 'sendmsg' in rpc:
                    start = rpc['sendmsg']
                    if 'recvmsg_done' in rpc:
                        end = rpc['recvmsg_done']
                    else:
                        end = traces[node]['last_time']
                elif 'recvmsg_done' in rpc:
                    start = traces[node]['first_time']
                    end = rpc['recvmsg_done']
                elif ('remaining' in rpc) or ('sent' in rpc):
                    start = traces[node]['first_time']
                    end =   traces[node]['last_time']
                if start != None:
                    add_to_intervals(node, start, end, 'rpcs_live', 1)

            # tx_live, rx_live
            if 'tx_live' in rpc:
                start, end = rpc['tx_live']
                if id & 1:
                    add_to_intervals(node, start, end, 'tx_live_resp', 1)
                else:
                    add_to_intervals(node, start, end, 'tx_live_req', 1)
            if 'rx_live' in rpc:
                start, end = rpc['rx_live']
                add_to_intervals(node, start, end, 'rx_live', 1)

            # tx_starts
            if 'sendmsg' in rpc:
                get_interval(node, rpc['sendmsg'])['tx_starts'] += 1

            # rx_starts
            if rpc['gro_data']:
                t, offset, prio = rpc['gro_data'][0]
                if offset == 0:
                    get_interval(node, t)['rx_starts'] += 1

            # tx_grant_avl
            self.add_grant_info(rpc)

            # rx_grantable
            in_length = rpc['in_length']
            if rpc['send_grant'] or (('unsched' in rpc) and (in_length != None)
                    and (in_length > rpc['unsched'])):
                start = traces[rpc['node']]['first_time']
                if rpc['softirq_data_pkts']:
                    start = rpc['softirq_data_pkts'][0]['softirq']

                end = traces[rpc['node']]['last_time']
                if rpc['send_grant_pkts']:
                    last_grant = rpc['send_grant_pkts'][-1]
                    if last_grant['offset'] >= rpc['in_length']:
                        end = last_grant['xmit']
                add_to_intervals(node, start, end, 'rx_grantable', 1)

        # Compute NIC queue lengths
        for node, xmits in node_xmits.items():
            if not xmits:
                continue
            xmits.sort(key=lambda t : t[0])
            cur_queue = 0
            prev_time = min(xmits[0][0], intervals[node][0]['time'])
            i = 0
            t, bytes = xmits[i]
            for interval in intervals[node]:
                itime = interval['time']
                while itime >= t:
                    cur_queue = self.qlen(cur_queue, t - prev_time) + bytes
                    prev_time = t
                    i += 1
                    if i >= len(xmits):
                        t = 1e20
                    else:
                        t, bytes = xmits[i]
                cur_queue = self.qlen(cur_queue, itime - prev_time)
                prev_time = itime
                interval['tx_q'] = cur_queue

#------------------------------------------------
# Analyzer: lost
#------------------------------------------------
class AnalyzeLost:
    """
    Prints information about packets that appear to have been dropped
    in the network.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')

    def analyze(self):
        global packets, traces

        # Packets that appear to have been lost.
        self.lost_pkts = []

        # node -> count of lost packets transmitted from that node.
        self.tx_lost = defaultdict(lambda : 0)

        # node-> count of lost packets destined for that node.
        self.rx_lost = defaultdict(lambda : 0)

        # node -> number of packets retransmitted
        self.retransmits = defaultdict(lambda : 0)

        # RPC id -> True for all RPCs with at least one outgoing packet
        # either lost or retransmitted.
        self.lost_rpcs = {}

        # tx_node -> dict {rx_node -> core where GRO will happen for packets
        # send from tx_node to rx_node}
        self.rx_core = defaultdict(dict)

        for pkt in packets.values():
            if not 'xmit' in pkt:
                continue
            rx_node = get_rpc_node(pkt['id']^1)
            if rx_node == None:
                continue
            if 'gro' in pkt:
                if not 'tx_node' in pkt:
                    print('Strange packet: %s' % (pkt))
                self.rx_core[pkt['tx_node']][rx_node] = pkt['gro_core']
                continue
            if (pkt['xmit'] + 200) > traces[rx_node]['last_time']:
                continue
            if pkt['xmit'] < traces[rx_node]['first_time']:
                continue
            self.lost_pkts.append(pkt)
            self.tx_lost[pkt['tx_node']] += 1
            self.rx_lost[rx_node] += 1

        for rpc in rpcs.values():
            self.retransmits[rpc['node']] += len(rpc['retransmits'])

    def output(self):
        global packets, rpcs, options, traces

        print('\n--------------')
        print('Analyzer: lost')
        print('--------------')
        print('Packets that appear to be lost: %d/%d (%.1f%%)' % (len(self.lost_pkts),
                len(packets), 100*len(self.lost_pkts)/len(packets)))
        num_retrans = sum(self.retransmits.values())
        print('Retransmitted packets: %d/%d (%.1f%%)' % (num_retrans,
                len(packets), 100*num_retrans/len(packets)))
        print('')

        print('A packet is considered to be "lost" if it has been transmitted')
        print('but there is no evidence that it was ever received (presumably')
        print('it has not been retransmitted).')
        print('Node:      Name of a node')
        print('TxLost:    Lost packets sent from this node')
        print('RxLost:    Lost packets destined to this node')
        print('Retrans:   Number of packets retransmitted by node')

        print('\nNode      TxLost  RxLost Retrans')
        print('--------------------------------')
        for node in get_sorted_nodes():
            print('%-10s %6d %6d  %6d' % (node, self.tx_lost[node],
                    self.rx_lost[node], self.retransmits[node]))

        print('\nXmit       TxNode     RxNode    RxCore        RpcId Offset')
        print('----------------------------------------------------------')
        prev_xmit = 1e20
        prev_tx_node = ''
        prev_rx_node = ''
        prev_core = -1
        prev_id = 0
        for pkt in sorted(self.lost_pkts, key=lambda p : p['xmit']):
            xmit = pkt['xmit']
            if xmit == prev_xmit:
                xmit_info = ''
            else:
                xmit_info = '%.3f' % (xmit)
                prev_xmit = xmit

            tx_node = pkt['tx_node']
            rx_node = get_rpc_node(pkt['id']^1)
            if not rx_node in self.rx_core[tx_node]:
                core_info = "???"
                prev_core = -1
            else:
                core = self.rx_core[tx_node][rx_node]
                if core == prev_core:
                    core_info = ''
                else:
                    core_info = '%d' % (core)
                    prev_core = core

            if tx_node == prev_tx_node:
                tx_node = ''
            else:
                prev_tx_node = tx_node

            if rx_node == prev_rx_node:
                rx_node = ''
            else:
                prev_rx_node = rx_node

            id = pkt['id']
            if id == prev_id:
                id_info = ''
            else:
                id_info = '%d' % (id)
                prev_id = id

            print('%9s   %-10s %-10s %4s %12s %6d' % (xmit_info, tx_node,
                    rx_node, core_info, id_info, pkt['offset']))

#------------------------------------------------
# Analyzer: net
#------------------------------------------------
class AnalyzeNet:
    """
    Prints information about delays in the network including NICs, network
    delay and congestion, and receiver GRO overload. With --data, generates
    data files describing backlog and delay over time on a core-by-core
    basis.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        return

    def collect_events(self):
        """
        Matches up packet sends and receives for all RPCs to return a
        dictionary that maps from the name for a receiving node to a
        list of events for that receiver. Each event is a
        <time, event, length, core, delay> list:
        time:      Time when the event occurred
        event:     What happened: "xmit" for packet transmission or "recv"
                   for packet reception (by GRO)
        length:    Number of message bytes in packet
        core:      Core where packet was processed by GRO
        delay:     End-to-end delay for packet; zero for xmit events
        """

        global rpcs, traces, options
        receivers = defaultdict(list)

        # Process RPCs in sender-receiver pairs to collect data
        for xmit_id, xmit_rpc in rpcs.items():
            recv_id = xmit_id ^ 1
            if not recv_id in rpcs:
                continue
            recv_rpc = rpcs[recv_id]
            receiver = receivers[recv_rpc['node']]
            if not 'gro_core' in recv_rpc:
                continue
            core = recv_rpc['gro_core']

            xmit_pkts = sorted(xmit_rpc['send_data'], key=lambda t : t[1])
            if xmit_pkts:
                xmit_end = xmit_pkts[-1][1] + xmit_pkts[-1][2]
            elif 'out_length' in xmit_rpc:
                xmit_end = xmit_rpc['out_length']
            elif 'in_length' in recv_rpc:
                xmit_end = recv_rpc['in_length']
            else:
                # Not enough info to process this RPC
                continue

            recv_pkts = sorted(recv_rpc['gro_data'],
                    key=lambda tuple : tuple[1])
            xmit_ix = 0
            if xmit_pkts:
                xmit_time, xmit_offset, xmit_length = xmit_pkts[0]
            else:
                xmit_offset = 100000000
                xmit_length = 0
            xmit_bytes = 0
            for i in range(0, len(recv_pkts)):
                recv_time, recv_offset, prio = recv_pkts[i]
                length = get_recv_length(recv_offset, xmit_end)

                while recv_offset >= (xmit_offset + xmit_length):
                    if xmit_bytes:
                        receiver.append([xmit_time, "xmit", xmit_bytes,
                                core, 0.0])
                    xmit_ix += 1
                    if xmit_ix >= len(xmit_pkts):
                        break
                    xmit_time, xmit_offset, xmit_length = xmit_pkts[xmit_ix]
                    xmit_bytes = 0
                if recv_offset < xmit_offset:
                    # No xmit record; skip
                    continue
                if xmit_ix >= len(xmit_pkts):
                    # Receiver trace extends beyond sender trace; ignore extras
                    break

                # Skip retransmitted packets (too hard to account for).
                retransmit = False
                for resend in recv_rpc['resend_rx']:
                    if resend[1] == recv_offset:
                        retransmit = True
                        break
                if retransmit or (recv_offset in xmit_rpc['retransmits']):
                    continue
                receiver.append([recv_time, "recv", length, core,
                        recv_time - xmit_time])
                if recv_time < xmit_time and not options.negative_ok:
                    print('%9.3f Negative delay, xmit_time %9.3f, '
                            'xmit_node %s recv_node %s recv_offset %d '
                            'xmit_offset %d xmit_length %d'
                            % (recv_time, xmit_time, xmit_rpc['node'],
                            recv_rpc['node'], recv_offset, xmit_offset,
                            xmit_length), file=sys.stderr)
                xmit_bytes += length
            if xmit_bytes:
                receiver.append([xmit_time, "xmit", xmit_bytes, core, 0.0])

        for name, receiver in receivers.items():
            receiver.sort(key=lambda tuple : tuple[0])
        return receivers

    def summarize_events(self, events):
        """
        Given a dictionary returned by collect_events, return information
        about each GRO core as a dictionary indexed by node names. Each
        element is a dictionary indexed by cores, which in turn is a
        dictionary with the following values:
        num_packets:      Total number of packets received by the core
        avg_delay:        Average end-to-end delay for packets
        max_delay:        Worst-case end-to-end delay
        max_delay_time:   Time when max_delay occurred
        avg_backlog:      Average number of bytes of data in transit
        max_backlog:      Worst-case number of bytes of data in transit
        max_backlog_time: Time when max_backlog occurred
        """
        global options

        stats = defaultdict(lambda: defaultdict(lambda: {
            'num_packets': 0,
            'avg_delay': 0,
            'max_delay': 0,
            'max_delay_time': 0,
            'avg_backlog': 0,
            'max_backlog': 0,
            'cur_backlog': 0,
            'prev_time': 0}))

        for name, node_events in events.items():
            node = stats[name]
            for event in node_events:
                time, type, length, core, delay = event
                core_data = node[core]
                core_data['avg_backlog'] += (core_data['cur_backlog'] *
                        (time - core_data['prev_time']))
                if type == "recv":
                    core_data['num_packets'] += 1
                    core_data['avg_delay'] += delay
                    if delay > core_data['max_delay']:
                        core_data['max_delay'] = delay
                        core_data['max_delay_time'] = time
                    if core_data['cur_backlog'] == core_data['max_backlog']:
                        core_data['max_backlog_time'] = time
                    core_data['cur_backlog'] -= length
                    if (delay < 0) and not options.negative_ok:
                        print('Negative delay: %s' % (event))
                else:
                    core_data['cur_backlog'] += length
                    if core_data['cur_backlog'] > core_data['max_backlog']:
                            core_data['max_backlog'] = core_data['cur_backlog']
                core_data['prev_time'] = time
            for core_data in node.values():
                core_data['avg_delay'] /= core_data['num_packets']
                core_data['avg_backlog'] /= traces[name]['elapsed_time']
        return stats

    def generate_delay_data(self, events, dir):
        """
        Creates data files for the delay information in events.

        events:    Dictionary of events returned by collect_events.
        dir:       Directory in which to write data files (one file per node)
        """

        for node, node_events in events.items():
            # Core number -> list of <time, delay> tuples for that core. Each
            # tuple indicates when a packet was processed by GRO on that core,
            # and the packet's end-to-end delay. The list for each core is
            # sorted in increasing time order.
            core_data = defaultdict(list)
            for event in node_events:
                event_time, type, length, core, delay = event
                if type != "recv":
                    continue
                core_data[core].append([event_time, delay])

            cores = sorted(core_data.keys())
            max_len = 0
            for core in cores:
                length = len(core_data[core])
                if length > max_len:
                    max_len = length

            f = open('%s/net_delay_%s.dat' % (dir, node), 'w')
            f.write('# Node: %s\n' % (node))
            f.write('# Generated at %s.\n' %
                    (time.strftime('%I:%M %p on %m/%d/%Y')))
            doc = ('# Packet delay information for a single node, broken '
                'out by the core '
                'where the packet is processed by GRO. For each active core '
                'there are two columns, TimeN and '
                'DelayN. Each line corresponds to a packet that was processed '
                'by homa_gro_receive on core N at the given time with '
                'the given delay '
                '(measured end to end from ip_*xmit call to homa_gro_receive '
                'call)')
            f.write('\n# '.join(textwrap.wrap(doc)))
            f.write('\n')
            for core in cores:
                t = 'Time%d' % core
                d = 'Delay%d' % core
                f.write('%8s%8s' % (t, d))
            f.write('\n')
            for i in range(0, max_len):
                for core in cores:
                    pkts = core_data[core]
                    if i >= len(pkts):
                        f.write('' * 15)
                    else:
                        f.write('%8.1f %7.1f' % (pkts[i][0], pkts[i][1]))
                f.write('\n')
            f.close()

    def generate_backlog_data(self, events, dir):
        """
        Creates data files for per-core backlog information

        events:    Dictionary of events returned by collect_events.
        dir:       Directory in which to write data files (one file per node)
        """
        global options

        for node, node_events in events.items():
            # Core number -> list; entry i in the list is the backlog on that
            # core at the end of interval i.
            backlogs = defaultdict(list)

            interval_length = 20.0
            start = (node_events[0][0]//interval_length) * interval_length
            interval_end = start + interval_length
            cur_interval = 0

            for event in node_events:
                event_time, type, length, core, delay = event
                while event_time >= interval_end:
                    interval_end += interval_length
                    cur_interval += 1
                    for core_intervals in backlogs.values():
                        core_intervals.append(core_intervals[-1])

                if not core in backlogs:
                    backlogs[core] = [0] * (cur_interval+1)
                if type == "recv":
                    backlogs[core][-1] -= length
                else:
                    backlogs[core][-1] += length

            cores = sorted(backlogs.keys())

            f = open('%s/net_backlog_%s.dat' % (dir, node), "w")
            f.write('# Node: %s\n' % (node))
            f.write('# Generated at %s.\n' %
                    (time.strftime('%I:%M %p on %m/%d/%Y')))
            doc = ('# Time-series history of backlog for each active '
                'GRO core on this node.  "Total" gives the total backlog '
                'across all cores, and "BackC" shows the backlog '
                'on core C at the given time (in usec). Backlog '
                'is the KB of data destined '
                'for a core that have been passed to ip*_xmit at the sender '
                'but not yet seen by homa_gro_receive on the receiver.')
            f.write('\n# '.join(textwrap.wrap(doc)))
            f.write('\n    Time   Total')
            for core in cores:
                f.write(' %7s' % ('Back%d' % core))
            f.write('\n')
            for i in range(0, cur_interval):
                f.write('%8.1f' % (start + (i+1)*interval_length))
                total = 0
                for core in cores:
                    total += backlogs[core][i] / 1000
                f.write(' %7.1f' % total)
                for core in cores:
                    f.write(' %7.1f' % (backlogs[core][i] / 1000))
                f.write('\n')
            f.close()

    def output(self):
        global rpcs, traces, options

        events = self.collect_events()

        if options.data != None:
            self.generate_delay_data(events, options.data)
            self.generate_backlog_data(events, options.data)

        stats = self.summarize_events(events)

        print('\n--------------')
        print('Analyzer: net')
        print('--------------')
        print('Network delay (including sending NIC, network, receiving NIC, and GRO')
        print('backup) for packets with GRO processing on a particular core.')
        print('Pkts:      Total data packets processed by Core on Node')
        print('AvgDelay:  Average end-to-end delay from ip_*xmit invocation to '
                'GRO (usec)')
        print('MaxDelay:  Maximum end-to-end delay, and the time when the max packet was')
        print('           processed by GRO (usec)')
        print('AvgBack:   Average backup for Core on Node (total data bytes that were')
        print('           passed to ip_*xmit but not yet seen by GRO) (KB)')
        print('MaxBack:   Maximum backup for Core (KB) and the time when GRO processed')
        print('           a packet from that backup')
        print('')
        print('Node       Core   Pkts  AvgDelay     MaxDelay (Time)     '
                'AvgBack    MaxBack (Time)')
        print('---------------------------------------------------------'
                '-----------------------------', end='')
        for name in get_sorted_nodes():
            if not name in stats:
                continue
            node = stats[name]
            print('')
            for core in sorted(node.keys()):
                core_data = node[core]
                print('%-10s %4d %6d %9.1f %9.1f (%10.3f) %8.1f %8.1f (%10.3f)' % (
                        name, core, core_data['num_packets'],
                        core_data['avg_delay'], core_data['max_delay'],
                        core_data['max_delay_time'],
                        core_data['avg_backlog'] * 1e-3,
                        core_data['max_backlog'] * 1e-3,
                        core_data['max_backlog_time']))

#------------------------------------------------
# Analyzer: ooo
#------------------------------------------------
class AnalyzeOoo:
    """
    Prints statistics about out-of-order packet arrivals. Also prints
    details about out-of-order packets in the RPCs that experienced the
    highest out-of-order delays (--verbose will print info for all OOO RPCs)
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')

    def output(self):
        global rpcs, options

        total_rpcs = 0
        total_packets = 0
        ooo_packets = 0

        # Each element of this list contains a <delay, info> tuple describing
        # all of the out-of-order packets in a single RPC: delay is the
        # maximum delay experienced by any of the out-of-order packets, and
        # info contains one or more lines of text, each line describing one
        # ooo packet.
        ooo_rpcs = []

        # Each element of this list represents one RPC whose completion
        # was delayed by ooo packets (i.e. the last packet received didn't
        # contain the last bytes of the message). Each element is a tuple
        # <delay, id, count>:
        # delay:   time between the arrival of the packet containing the
        #          last bytes of the message and the arrival of the last
        #          packet
        # id:      RPC identifier
        # count:   the number of packets that arrived after the one containing
        #          the last bytes of the message
        delayed_msgs = []

        # Scan the incoming packets in each RPC.
        for id, rpc in rpcs.items():
            if not 'gro_data' in rpc:
                continue
            total_rpcs += 1
            pkts = rpc['gro_data']
            total_packets += len(pkts)
            highest_index = -1
            highest_offset = -1
            highest_offset_time = 0
            last_time = 0
            packets_after_highest = 0
            highest_prio = 0
            max_delay = -1
            info = ''
            for i in range(len(pkts)):
                time, offset, prio = pkts[i]
                last_time = time
                if offset > highest_offset:
                    highest_index = i;
                    highest_offset = offset
                    highest_offset_time = time
                    highest_prio = prio
                    packets_after_highest = 0
                    continue
                else:
                    packets_after_highest += 1

                # This packet is out of order. Find the first packet received
                # with higher offset than this one so we can compute how long
                # this packet was delayed.
                ooo_packets += 1
                gap = highest_index
                while gap > 0:
                    if pkts[gap-1][1] < offset:
                        break
                    gap -= 1
                gap_time, gap_offset, gap_prio = pkts[gap]
                delay = time - gap_time
                if max_delay == -1:
                    rpc_id = '%12d' % (id)
                else:
                    rpc_id = ' ' * 12
                info += '%s %7d %10s %9.3f %7.1f %8d  %3d  %3d\n' % (rpc_id, offset,
                        rpc['node'], time, delay, highest_offset - offset,
                        prio, highest_prio)
                if delay > max_delay:
                    max_delay = delay
            if info:
                ooo_rpcs.append([max_delay, info])
            if packets_after_highest > 0:
                delayed_msgs.append([last_time - highest_offset_time, id,
                        packets_after_highest])

        print('\n-----------------')
        print('Analyzer: ooo')
        print('-----------------')
        print('Messages with out-of-order packets: %d/%d (%.1f%%)' %
                (len(ooo_rpcs), total_rpcs, 100.0*len(ooo_rpcs)/total_rpcs))
        print('Out-of-order packets: %d/%d (%.1f%%)' %
                (ooo_packets, total_packets, 100.0*ooo_packets/total_packets))
        if delayed_msgs:
            delayed_msgs.sort()
            print('')
            print('Messages whose completion was delayed by out-of-order-packets: '
                    '%d (%.1f%%)' % (len(delayed_msgs),
                    100.0*len(delayed_msgs)/len(rpcs)))
            print('P50 completion delay: %.1f us' % (
                    delayed_msgs[len(delayed_msgs)//2][0]))
            print('P90 completion delay: %.1f us' % (
                    delayed_msgs[(9*len(delayed_msgs))//10][0]))
            print('Worst delays:')
            print('Delay (us)         RPC   Receiver  Late Pkts')
            for i in range(len(delayed_msgs)-1, len(delayed_msgs)-6, -1):
                if i < 0:
                    break;
                delay, id, pkts = delayed_msgs[i]
                print('  %8.1f  %10d %10s      %5d' %
                        (delay, id, rpcs[id]['node'], pkts))

            delayed_msgs.sort(key=lambda t : t[2])
            packets_sum = sum(i[2] for i in delayed_msgs)
            print('Late packets per delayed message: P50 %.1f, P90 %.1f, Avg %.1f' %
                    (delayed_msgs[len(delayed_msgs)//2][2],
                    delayed_msgs[(9*len(delayed_msgs))//10][2],
                    packets_sum / len(delayed_msgs)))
        else:
            print('No RPCs had their completion delayed by out-of-order packtets')

        if not ooo_rpcs:
            return
        print('')
        print('Information about out-of-order packets, grouped by RPC and sorted')
        print('so that RPCs with largest OOO delays appear first (use --verbose')
        print('to display all RPCs with OOO packets):')
        print('RPC:     Identifier for the RPC')
        print('Offset:  Offset of the out-of-order packet within the RPC')
        print('Node:    Node on which the packet was received')
        print('Time:    Time when the packet was received by homa_gro_receive')
        print('Delay:   Time - receive time for earliest packet with higher offset')
        print('Gap:     Offset of highest packet received before this one, minus')
        print('         offset of this packet')
        print('Prio:    Priority of this packet')
        print('Prev:    Priority of the highest-offset packet received before ')
        print('         this one')
        print('')
        print('         RPC  Offset       Node      Time   Delay      Gap Prio Prev')
        print('--------------------------------------------------------------------')
        ooo_rpcs.sort(key=lambda t : t[0], reverse=True)
        count = 0
        for delay, info in ooo_rpcs:
            if (count >= 20) and not options.verbose:
                break
            print(info, end='')
            count += 1

#------------------------------------------------
# Analyzer: packet
#------------------------------------------------
class AnalyzePacket:
    """
    Analyzes the delay between when a particular packet was sent and when
    it was received by GRO: prints information about other packets competing
    for the same GRO core. Must specify the packet of interest with the
    '--pkt ID:offset' option: this is the packet id on the sender.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')
        return

    def output(self):
        global rpcs, traces, options, peer_nodes, packets

        print('\n-----------------')
        print('Analyzer: packet')
        print('-----------------')
        if not options.pkt:
            print('Skipping packet analyzer: --pkt not specified',
                    file=sys.stderr)
            return

        if not options.pkt in packets:
            print('Can\'t find packet %s' % (options.pkt),
                    file=sys.stderr)
            return
        pkt = packets[options.pkt]
        for field in ['gro', 'priority', 'xmit']:
            if not field in pkt:
                print('Packet %s doesn\'t have a "%s" field' % (options.pkt,
                        field), file=sys.stderr)
                return
        xmit_time = pkt['xmit']
        xmit_id = pkt['id']
        recv_time = pkt['gro']
        rx_node = pkt['rx_node']
        gro_core = pkt['gro_core']
        tx_node = pkt['tx_node']

        print('Packet: RPC id %d, offset %d, delay %6.1f us' % (xmit_id,
                options.pkt_offset, recv_time - xmit_time))
        print('%.3f: Packet passed to ip*xmit on %s, core %d' % (xmit_time,
                tx_node, pkt['tx_core']))
        if 'nic' in pkt:
            print('%.3f: Packet transmitted by NIC on %s' % (pkt['nic'],
                    tx_node))
        print('%.3f: Packet received by %s on core %d with priority %d'
                % (recv_time, pkt['rx_node'], pkt['gro_core'], pkt['priority']))

        # List of packets received by rx_node whose lifetimes overlap
        # the reference packet.
        pkts = []

        # Amount of data already in transit to target at the time reference
        # packet was transmitted.
        prior_bytes = 0
        prior_pkts = 0

        for p in packets.values():
            if not 'gro' in p:
                continue
            if p['gro'] < xmit_time:
                continue
            if p['rx_node'] != rx_node:
                continue
            if p is pkt:
                continue
            if 'xmit' in p:
                if p['xmit'] >= recv_time:
                    continue
                if p['xmit'] <= xmit_time:
                    prior_bytes += p['length']
                    prior_pkts += 1
            elif p['gro'] >= recv_time:
                    continue
            pkts.append(p)

        # Amount of data transmitted after the reference packet but received
        # on the reference packet's core before the reference packet.
        after_core_bytes = 0
        after_core_pkts = 0

        # Amount of data transmitted after the reference packet but received
        # on other cores before the reference packet.
        after_other_bytes = 0
        after_other_pkts = 0

        # Create output messages grouped into categories.
        pkts.sort(key=lambda p : p['gro'])
        before_before = ''
        before_after = ''
        after_before_core = ''
        after_before_other = ''
        after_after = ''
        unknown_before = ''
        for p in pkts:
            if not 'xmit' in p:
                sender = ""
                if p['id'] in rpcs:
                    sender = rpcs[p['id']]['node']
                unknown_before += ('\n      ???  %9.3f      ??? %11d  %7d '
                        '%-10s %4s %6d    %2d  %4d' % (p['gro'], p['id'],
                        p['offset'], sender, "", p['length'],
                        p['priority'], p['gro_core']))
                continue
            msg = '\n%9.3f  %9.3f %8.1f %11d  %7d %-10s %4s %6d    %2d  %4d' %  (
                    p['xmit'], p['gro'], p['gro'] - p['xmit'], p['id'],
                    p['offset'], p['tx_node'], p['tx_core'], p['length'],
                    p['priority'], p['gro_core'])
            if p['xmit'] < xmit_time:
                if p['gro'] < recv_time:
                    before_before += msg
                else:
                    before_after += msg
            else:
                if p['gro'] < recv_time:
                    if p['gro_core'] == gro_core:
                        after_before_core += msg
                        after_core_bytes += p['length']
                        after_core_pkts += 1
                    else:
                        after_before_other += msg
                        after_other_bytes += p['length']
                        after_other_pkts += 1
                else:
                    after_after += msg

        print('%.1f KB (%d packets) already in transit to %s when packet '
                'transmitted' % (prior_bytes * 1e-3, prior_pkts, tx_node))
        print('             (%.1f us at %.0f Gbps)' % (
                bytes_to_usec(prior_bytes), options.gbps))
        print('%.1f KB (%d packets) transmitted to core %d after packet was '
                'transmitted but' % (after_core_bytes * 1e-3, after_core_pkts,
                gro_core))
        print('             received before packet (%.1f us at %.0f Gbps)'
                % (bytes_to_usec(after_core_bytes),  options.gbps))
        print('%.1f KB (%d packets) transmitted to other cores after packet '
                'was' % (after_other_bytes * 1e-3,
                after_other_pkts))
        print('             transmitted but received before packet (%.1f us '
                'at %.0f Gbps)' % (bytes_to_usec(after_other_bytes),
                options.gbps))
        print('\nOther packets whose transmission to %s overlapped this '
                'packet:' % (rx_node))
        print('Xmit:     Time packet was transmitted')
        print('Recv:     Time packet was received on core %d' % (gro_core))
        print('Delay:    End-to-end latency for packet')
        print('Rpc:      Id of packet\'s RPC (on sender)')
        print('Offset:   Offset of packet within message')
        print('Sender:   Node that sent packet')
        print('TxCore:   Core on which packet was passed to ip*xmit')
        print('Length:   Number of message bytes in packet')
        print('Prio:     Priority at which packet was transmitted')
        print('Core:     Core on which homa_gro_receive handled packet')
        hdr = '     Xmit       Recv    Delay         Rpc   Offset Sender   ' \
                'TxCore Length  Prio  Core\n' \
                '------------------------------------------------------------' \
                '-------------------------'
        if before_before:
            print('\nSent before %s, received before:\n%s\n%s' %
                    (options.pkt, hdr, before_before))
        if before_after:
            print('\nSent before %s, received after:\n%s\n%s' %
                    (options.pkt, hdr, before_after))
        if after_before_core:
            print('\nSent after %s, received on core %d before:\n%s\n%s' %
                    (options.pkt, gro_core, hdr, after_before_core))
        if after_before_other:
            print('\nSent after %s, received on other cores before:\n%s\n%s' %
                    (options.pkt, hdr, after_before_other))
        if after_after:
            print('\nSent after %s, received after:\n%s\n%s' %
                    (options.pkt, hdr, after_after))
        if unknown_before:
            print('\nSend time unknown, received before:\n%s\n%s' % (hdr,
                    unknown_before))

#------------------------------------------------
# Analyzer: packets
#------------------------------------------------
class AnalyzePackets:
    """
    Collects information about each data packet and grant but doesn't
    generate any output. The data it collects is used by other analyzers.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')

        # offset -> Largest length that has occurred for that offset in a
        # TSO packet.  Used to compute tso_offset field if it is
        # missing
        self.tso_lengths = defaultdict(lambda : -1)
        return

    def init_trace(self, trace):
        # RPC id -> list of live data packets for that RPC (packets that
        # have been received by homa_gro_receive but not yet copied to user
        # space).
        self.live = defaultdict(list)

        # Core -> list of packets that have been copied out to user space by
        # that core (but not yet freed).
        self.copied = defaultdict(list)

    def tt_ip_xmit(self, trace, t, core, id, offset):
        global packets, rpcs
        p = packets[pkt_id(id, offset)]
        # If packet retransmitted, only record first transmission
        if not 'retransmits' in p:
            p['xmit'] = t
            p['tx_node'] = trace['node']
            p['tx_core'] = core
            rpcs[id]['send_data_pkts'].append(p)

    def tt_mlx_data(self, trace, t, core, peer, id, offset):
        global packets
        p = packets[pkt_id(id, offset)]
        if not 'retransmits' in p:
            p['nic'] = t
            p['tx_node'] = trace['node']

    def tt_free_tx_skb(self, trace, t, core, id, offset, qid, msg_length):
        global packets
        p = packets[pkt_id(id, offset)]
        p['free_tx_skb'] = t
        p['tx_qid'] = qid
        p['tx_node'] = trace['node']
        p['msg_length'] = msg_length

    def tt_gro_data(self, trace, t, core, peer, id, offset, prio):
        global packets, recv_offsets, rpcs
        p = packets[pkt_id(id^1, offset)]
        if not 'gro' in p:
            rpcs[id]['gro_data_pkts'].append(p)
        p['gro'] = t
        p['priority'] = prio
        p['gro_core'] = core
        p['rx_node'] = trace['node']
        recv_offsets[offset] = True
        self.live[id^1].append(p)

    def tt_softirq_data(self, trace, t, core, id, offset, msg_length):
        global packets, rpcs
        p = packets[pkt_id(id^1, offset)]
        if not 'softirq' in p:
            rpcs[id]['softirq_data_pkts'].append(p)
        p['softirq'] = t
        p['softirq_core'] = core
        p['msg_length'] = msg_length
        p['rx_node'] = trace['node']

    def tt_copy_out_done(self, trace, t, core, id, start, end):
        pkts = self.live[id^1]
        for i in range(len(pkts) -1, -1, -1):
            p = pkts[i]
            if (p['offset'] >= start) and (p['offset'] < end):
                p['copied'] = t
                self.copied[core].append(p)
                pkts.pop(i)

    def tt_free_skbs(self, trace, t, core, num_skbs):
        for p in self.copied[core]:
            p['free'] = t
        self.copied[core] = []

    def tt_send_data(self, trace, t, core, id, offset, length):
        global packets
        p = packets[pkt_id(id, offset)]
        if (not 'retransmits' in p) and (length > self.tso_lengths[offset]):
            self.tso_lengths[offset] = length
        p['id'] = id
        # If packet has been retransmitted, don't record tso_length, since
        # that could make a TSO segment appear to be the main TSO packet.
        if not 'retransmits' in p:
            p['tso_length'] = length

    def tt_retransmit(self, trace, t, core, id, offset, length):
        global packets
        p = packets[pkt_id(id, offset)]
        if not 'retransmits' in p:
            p['retransmits'] = [t]
        else:
            p['retransmits'].append(t)

    def tt_send_grant(self, trace, t, core, id, offset, priority, increment):
        global grants, rpcs
        g = grants[pkt_id(id, offset)]
        if not 'xmit' in g:
            rpcs[id]['send_grant_pkts'].append(g)
        g['xmit'] = t
        g['tx_node'] = trace['node']
        g['increment'] = increment

    def tt_mlx_grant(self, trace, t, core, peer, id, offset):
        global grants
        g = grants[pkt_id(id, offset)]
        g['nic'] = t
        g['tx_node'] = trace['node']

    def tt_gro_grant(self, trace, t, core, peer, id, offset, priority):
        global grants
        g = grants[pkt_id(id^1, offset)]
        if not 'gro' in g:
            rpcs[id]['gro_grant_pkts'].append(g)
        g['gro'] = t
        g['gro_core'] = core

    def tt_softirq_grant(self, trace, t, core, id, offset, priority, increment):
        global grants
        g = grants[pkt_id(id^1, offset)]
        if not 'softirq' in g:
            rpcs[id]['softirq_grant_pkts'].append(g)
        g['softirq'] = t
        g['softirq_core'] = core
        g['increment'] = increment

    def analyze(self):
        """
        Try to deduce missing packet fields, such as message length.
        """
        global packets, rpcs, grants
        sync_error_printed = False

        missing_rpc = {'send_data': []}
        new_pkts = []
        for pkt in packets.values():
            id = pkt['id']
            if id in rpcs:
                tx_rpc = rpcs[id]
            else:
                tx_rpc = missing_rpc
            if not 'msg_length' in pkt:
                pkt['msg_length'] = None
                if 'out_length' in tx_rpc:
                    pkt['msg_length'] = tx_rpc['out_length']
                elif id^1 in rpcs:
                    rx_rpc = rpcs[id^1]
                    if rx_rpc['in_length'] != None:
                        pkt['msg_length'] = rx_rpc['in_length']
            pkt['length'] = get_recv_length(pkt['offset'], pkt['msg_length'])
            if ('xmit' in pkt) and ('gro' in pkt) and (
                    (pkt['gro'] - pkt['xmit']) < -5.0):
                if not sync_error_printed:
                    print('Timetraces don\'t appear to be synchronized '
                            '(did you run ttsync.py?); packet arrived before '
                            'transmitted: %s' % (pkt),
                            file=sys.stderr)
                    sync_error_printed = True

            if not 'tso_length' in pkt:
                offset = pkt['offset']
                if offset in self.tso_lengths:
                    tso_length = self.tso_lengths[offset]
                    msg_length = pkt['msg_length']
                    if (msg_length != None) and ((offset + tso_length) > msg_length):
                        pkt['tso_length'] = msg_length - offset
                    else:
                        pkt['tso_length'] = tso_length

            # Make sure that all of the smaller packets deriving from each
            # TSO packet are represented and properly populated (if one of
            # these packets is lost it won't be represented yet).
            if 'tso_length' in pkt:
                tso_length = pkt['tso_length']
                if tso_length < pkt['length']:
                    pkt['length'] = tso_length
                offset = pkt['offset']
                id = pkt['id']
                end = tso_length + offset
                offset += get_recv_length(offset, end)
                while offset < end:
                    pid = pkt_id(id, offset)
                    length = get_recv_length(offset, end)
                    if pid in packets:
                        pkt2 = packets[pid]
                    else:
                        pkt2 = {'offset': offset, 'length': length}
                        new_pkts.append([pid, pkt2])
                    for key in ['xmit', 'nic', 'id', 'msg_length',
                                'priority', 'tx_node', 'tx_core',
                                'free_tx_skb']:
                        if key in pkt:
                            pkt2[key] = pkt[key]
                    offset += length
                    if not 'segments' in pkt:
                        pkt['segments'] = [pkt2]
                    else:
                        pkt['segments'].append(pkt2)
        for pid, pkt in new_pkts:
            packets[pid] = pkt

#------------------------------------------------
# Analyzer: rpcs
#------------------------------------------------
class AnalyzeRpcs:
    """
    Collects information about each RPC but doesn't actually print
    anything. Intended for use by other analyzers.
    """

    def __init__(self, dispatcher):
        return

    def append(self, trace, id, t, name, value):
        """
        Add a value to an element of an RPC's dictionary, creating the RPC
        and the list if they don't exist already

        trace:      Overall information about the trace file being parsed.
        id:         Identifier for a specific RPC; stats for this RPC are
                    initialized if they don't already exist
        t:          Time of the current event
        name:       Name of a value in the RPC's record; will be created
                    if it doesn't exist
        value:      Value to append to the list indicated by id and name
        """

        global rpcs
        rpc = rpcs[id]
        if not name in rpc:
            rpc[name] = []
        rpc[name].append(value)

    def tx_end(self, rpc):
        """
        Returns the end of the tx_live interval for RPC; this may be the
        last_time in the trace if transmission was incomplete at the end
        of the trace, or None if there doesn't appear to be any tx activity
        for RPC during the traces.
        """

        if not 'sent' in rpc and (not rpc['send_data']):
            return None

        ceiling = traces[rpc['node']]['last_time']
        if 'free' in rpc:
            ceiling = rpc['free']
        if not (rpc['id'] ^ 1):
            if rpc['gro_data']:
                ceiling = rpc['gro_data'][0][0]
            elif 'recvmsg_done' in rpc:
                ceiling = rpc['recmvsg_done']
        if rpc['send_data']:
            if ceiling != None:
                return rpc['send_data'][-1][0]
            if rpc['send_data'][-1][2] < 1500:
                return rpc['send_data'][-1][0]
            if 'out_length' in rpc:
                length = rpc['out_length']
                for t, offset, pkt_length in rpc['send_data']:
                    if (offset + pkt_length) >= length:
                        return rpc['send_data'][-1][0]
        return ceiling

    def set_live(self, rpc, peer):
        """
        Sets the rx_live and tx_live fields in the given RPC. Peer is
        the matching RPC on the peer node, or None if none.
        """

        global rpcs

        # tx_live
        node = rpc['node']
        end = self.tx_end(rpc)
        start = None
        if 'sendmsg' in rpc:
            start = rpc['sendmsg']
        if start != None:
            if end != None:
                rpc['tx_live'] = [start, end]
            else:
                rpc['tx_live'] = [start, traces[node]['last_time'],]
        elif end != None:
            rpc['tx_live'] = [traces[node]['first_time'], end]

        # rx_live
        start = None
        if peer and peer['send_data']:
                start = peer['send_data'][0][0]
        if start == None:
            if rpc['gro_data']:
                start = rpc['gro_data'][0][0]
            elif 'remaining' in rpc:
                start = traces[node]['first_time']
        if 'recvmsg_done' in rpc:
            end = rpc['recvmsg_done']
        elif 'remaining' in rpc:
            end = traces[node]['last_time']
        elif (start != None) and (start >= traces[node]['first_time']):
            end = traces[node]['last_time']
        else:
            end = None
        if (start != None) and (end != None):
            rpc['rx_live'] = [start, end]

    def tt_gro_data(self, trace, t, core, peer, id, offset, prio):
        global rpcs, recv_offsets
        self.append(trace, id, t, 'gro_data', [t, offset, prio])
        rpcs[id]['peer'] = peer
        rpcs[id]['gro_core'] = core
        recv_offsets[offset] = True

    def tt_gro_grant(self, trace, t, core, peer, id, offset, priority):
        self.append(trace, id, t, 'gro_grant', [t, offset])
        rpcs[id]['peer'] = peer
        rpcs[id]['gro_core'] = core

    def tt_rpc_handoff(self, trace, t, core, id):
        rpcs[id]['handoff'] = t
        rpcs.pop('queued', None)

    def tt_ip_xmit(self, trace, t, core, id, offset):
        global rpcs
        rpcs[id]['ip_xmits'][offset] = t

    def tt_rpc_queued(self, trace, t, core, id):
        rpcs[id]['queued'] = t
        rpcs.pop('handoff', None)

    def tt_resend_rx(self, trace, t, core, id, offset, length):
        global rpcs
        rpcs[id]['resend_rx'].append([t, offset, length])

    def tt_resend_tx(self, trace, t, core, id, offset):
        global rpcs
        rpcs[id]['resend_tx'].append([t, offset])

    def tt_retransmit(self, trace, t, core, id, offset, length):
        global rpcs
        rpcs[id]['retransmits'][offset] = [t, length]

    def tt_softirq_data(self, trace, t, core, id, offset, length):
        global rpcs
        self.append(trace, id, t, 'softirq_data', [t, offset])
        rpcs[id]['in_length'] = length

    def tt_softirq_grant(self, trace, t, core, id, offset, priority, increment):
        self.append(trace, id, t, 'softirq_grant', [t, offset])

    def tt_send_data(self, trace, t, core, id, offset, length):
        # Combine the length and other info from this record with the time
        # from the ip_xmit call. No ip_xmit call? Skip this record too.
        global rpcs
        if not offset in rpcs[id]['ip_xmits']:
            return
        ip_xmits = rpcs[id]['ip_xmits']
        self.append(trace, id, t, 'send_data', [ip_xmits[offset], offset, length])
        del ip_xmits[offset]

    def tt_send_grant(self, trace, t, core, id, offset, priority, increment):
        self.append(trace, id, t, 'send_grant', [t, offset, priority, increment])

    def tt_sendmsg_request(self, trace, t, core, peer, id, length):
        global rpcs
        rpcs[id]['out_length'] = length
        rpcs[id]['peer'] = peer
        rpcs[id]['sendmsg'] = t

    def tt_sendmsg_response(self, trace, t, core, id, length):
        global rpcs
        rpcs[id]['sendmsg'] = t
        rpcs[id]['out_length'] = length

    def tt_recvmsg_done(self, trace, t, core, id, length):
        global rpcs
        rpcs[id]['recvmsg_done'] = t

    def tt_wait_found_rpc(self, trace, t, core, id):
        rpcs[id]['found'] = t

    def tt_copy_out_start(self, trace, t, core, id):
        global rpcs
        if not 'copy_out_start' in rpcs[id]:
            rpcs[id]['copy_out_start'] = t

    def tt_copy_out_done(self, trace, t, core, id, start, end):
        global rpcs
        rpcs[id]['copy_out_done'] = t

    def tt_copy_in_done(self, trace, t, core, id, num_bytes):
        global rpcs
        rpcs[id]['copy_in_done'] = t

    def tt_unsched(self, trace, t, core, id, num_bytes):
        global rpcs, max_unsched
        rpcs[id]['unsched'] = num_bytes
        if num_bytes > max_unsched:
            max_unsched = num_bytes

    def tt_rpc_free(self, trace, t, core, id):
        global rpcs
        rpcs[id]['free'] = t

    def tt_rpc_incoming(self, trace, t, core, id, peer, received, length):
        global rpcs, max_unsched
        rpc = rpcs[id]
        rpc['peer'] = peer
        rpc['in_length'] = length
        rpc['remaining'] = length - received

    def tt_rpc_incoming2(self, trace, t, core, id, incoming, granted, prio):
        global rpcs, max_unsched
        rpcs[id]['granted'] = granted

    def tt_rpc_outgoing(self, trace, t, core, id, sent, length):
        global rpcs, max_unsched
        rpc = rpcs[id]
        rpc['out_length'] = length
        rpc['sent'] = sent

    def analyze(self):
        """
        Fill in various additional information related to RPCs
        """
        global rpcs, traces, peer_nodes

        for id, rpc in rpcs.items():
            peer_id = id ^ 1
            if peer_id in rpcs:
                peer_rpc = rpcs[peer_id]
            else:
                peer_rpc = None

            # Fill in peer_nodes
            if 'peer' in rpc:
                peer = rpc['peer']
                if (not peer in peer_nodes) and peer_rpc:
                    peer_nodes[peer] = peer_rpc['node']

            # Deduce out_length if not already present.
            if not 'out_length' in rpc:
                if peer_rpc and (peer_rpc['in_length'] != None):
                    rpc['out_length'] = peer_rpc['in_length']
                else:
                    length = -1
                    if 'send_data' in rpc:
                        for t, offset, pkt_length in rpc['send_data']:
                            l2 = offset + pkt_length
                            if l2 > length:
                                length = l2
                    if 'softirq_grant' in rpc:
                        for t, offset in rpc['softirq_grant']:
                            if offset > length:
                                length = offset
                    if length >= 0:
                        rpc['out_length'] = length

            # Set rx_live and tx_live
            self.set_live(rpc, peer_rpc)

        # Deduce in_length if not already present.
        for id, rpc in rpcs.items():
            if rpc['in_length'] == None:
                sender_id = id^1
                if sender_id in rpcs:
                    sender = rpcs[sender_id]
                    if 'out_length' in sender:
                        rpc['in_length'] = sender['out_length']

#------------------------------------------------
# Analyzer: rtt
#------------------------------------------------
class AnalyzeRtt:
    """
    Prints statistics about round-trip times for short RPCs and identifies
    RPCs with the longest RTTs. The --max-rtt option can be used to restrict
    the time range for the "long" RPCs to print out.
    """
    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        return

    def output(self):
        global rpcs, peer_nodes, options

        # List with one entry for each short RPC, containing a tuple
        # <rtt, id, start, end, client, server> where rtt is the round-trip
        # t, id is the client's RPC id, start and end are the beginning
        # and ending times, and client and server are the names of the two
        # nodes involved.
        rtts = []

        for id, rpc in rpcs.items():
            if id & 1:
                continue
            if (not 'sendmsg' in rpc) or (not 'recvmsg_done' in rpc):
                continue
            if (not 'out_length' in rpc) or (rpc['out_length'] > 1500):
                continue
            if (rpc['in_length'] == None) or (rpc['in_length'] > 1500):
                continue
            rtts.append([rpc['recvmsg_done'] - rpc['sendmsg'], id,
                    rpc['sendmsg'], rpc['recvmsg_done'], rpc['node'],
                    peer_nodes[rpc['peer']]])

        rtts.sort(key=lambda t : t[0])

        print('\n-------------')
        print('Analyzer: rtt')
        print('-------------')
        if not rtts:
            print('Traces contained no short RPCs (<= 1500 bytes)')
            return
        print('Round-trip times for %d short RPCs (<= 1500 bytes):'
                % (len(rtts)))
        print('Min:  %6.1f' % rtts[0][0])
        print('P10:  %6.1f' % rtts[10*len(rtts)//100][0])
        print('P50:  %6.1f' % rtts[50*len(rtts)//100][0])
        print('P90:  %6.1f' % rtts[90*len(rtts)//100][0])
        print('P99:  %6.1f' % rtts[99*len(rtts)//100][0])
        print('Max:  %6.1f' % rtts[len(rtts) - 1][0])

        def get_phase(rpc1, phase1, rpc2, phase2):
            """
            Returns the elapsed time from phase1 in rpc1 to phase2 in
            rpc2, or None if the required data is missing.
            """
            if phase1 not in rpc1:
                return None
            start = rpc1[phase1]
            if type(start) == list:
                if not start:
                    return None
                start = start[0][0]
            if phase2 not in rpc2:
                return None
            end = rpc2[phase2]
            if type(end) == list:
                if not end:
                    return None
                end = end[0][0]
            return end - start

        def get_phases(crpc, srpc):
            """
            Returns a dictionary containing the delays for each phase in
            the RPC recorded on the client side in crpc and the server side
            in srpc. Each phase measures from the end of the previous phase;
            if data wasn't available for a phase then the value will be None.
            prep:       From sendmsg until call to ip*xmit on client
            net:        To GRO on the server
            gro:        To SoftIRQ on the server
            softirq:    To homa_rpc_handoff
            handoff:    Handoff to waiting thread
            queue:      Wait on queue for receiving thread (alternative to
                        handoff: one of these will be None)
            sendmsg:    To sendmsg call on server
            prep2:      To call to ip*xmit on server
            net2:       To GRO on the client
            gro2:       To SoftIRQ on the client
            softirq2:   To homa_rpc_handoff on client
            handoff2:   Handoff to waiting thread
            queue2:     Wait on queue for receiving thread (only one of
                        this and handoff2 will be set)
            done:       To return from sendmsg on client
            """
            global rpcs

            result = {}

            result['prep'] = get_phase(crpc, 'sendmsg', crpc, 'send_data')
            result['net'] =  get_phase(crpc, 'send_data', srpc, 'gro_data')
            result['gro'] = get_phase(srpc, 'gro_data', srpc, 'softirq_data')
            if 'queued' in srpc:
                result['softirq'] = get_phase(srpc, 'softirq_data', srpc, 'queued')
                if result['softirq'] < 0:
                    result['softirq'] = 0
                result['queue'] = get_phase(srpc, 'queued', srpc, 'found')
                result['handoff'] = None
            else:
                result['softirq'] = get_phase(srpc, 'softirq_data', srpc, 'handoff')
                if result['softirq'] < 0:
                    result['softirq'] = 0
                result['handoff'] = get_phase(srpc, 'handoff', srpc, 'found')
                result['queue'] = None
            result['sendmsg'] = get_phase(srpc, 'found', srpc, 'sendmsg')
            result['prep2'] = get_phase(srpc, 'sendmsg', srpc, 'send_data')
            result['net2'] =  get_phase(srpc, 'send_data', crpc, 'gro_data')
            result['gro2'] = get_phase(crpc, 'gro_data', crpc, 'softirq_data')
            if 'queued' in crpc:
                result['softirq2'] = get_phase(crpc, 'softirq_data', crpc, 'queued')
                if result['softirq2'] < 0:
                    result['softirq2'] = 0
                result['queue2'] = get_phase(crpc, 'queued', crpc, 'found')
                result['handoff2'] = None
            else:
                result['softirq2'] = get_phase(crpc, 'softirq_data', crpc, 'handoff')
                if result['softirq2'] < 0:
                    result['softirq2'] = 0
                result['handoff2'] = get_phase(crpc, 'handoff', crpc, 'found')
                result['queue2'] = None
            result['done'] = get_phase(crpc, 'found', crpc, 'recvmsg_done')
            return result

        print('\nShort RPCs with the longest RTTs:')
        print('RTT:       Round-trip time (usecs)')
        print('Client Id: RPC id as seen by client')
        print('Server:    Node that served the RPC')
        print('Start:     Time of sendmsg invocation on client')
        print('Prep:      Time until request passed to ip*xmit')
        print('Net:       Time for request to reach server GRO')
        print('GRO:       Time to finish GRO and wakeup homa_softirq on server')
        print('SIRQ:      Time until server homa_softirq invokes homa_rpc_handoff')
        print('Handoff:   Time to pass RPC to waiting thread (if thread waiting)')
        print('Queue:     Time RPC is enqueued until receiving thread arrives')
        print('App:       Time until application wakes up and invokes sendmsg '
                'for response')
        print('Prep2:     Time until response passed to ip*xmit')
        print('Net2:      Time for response to reach client GRO')
        print('GRO2:      Time to finish GRO and wakeup homa_softirq on client')
        print('SIRQ2:     Time until client homa_softirq invokes homa_rpc_handoff')
        print('Hand2:     Time to pass RPC to waiting thread (if thread waiting)')
        print('Queue2:    Time RPC is enqueued until receiving thread arrives')
        print('Done:      Time until recvmsg returns on client')
        print('')
        print('   RTT    Client Id     Server     Start Prep    Net   GRO SIRQ '
                'Handoff  Queue  App Prep2   Net2   GRO2 SIRQ2  Hand2 Queue2 Done')
        print('----------------------------------------------------------------'
                '----------------------------------------------------------------')
        slow_phases = []
        slow_rtt_sum = 0
        to_print = 20
        max_rtt = 1e20
        if options.max_rtt != None:
            max_rtt = options.max_rtt
        for i in range(len(rtts)-1, -1, -1):
            rtt, id, start, end, client, server = rtts[i]
            if rtt > max_rtt:
                continue
            crpc = rpcs[id]
            server_id = id ^ 1
            if not server_id in rpcs:
                continue
            srpc = rpcs[server_id]
            phases = get_phases(crpc, srpc)
            slow_phases.append(phases)
            slow_rtt_sum += rtt

            def fmt_phase(phase, size=6):
                if (phase == None):
                    return ' '*size
                else:
                    return ('%' + str(size) + '.1f') % (phase)

            print('%6.1f %12d %10s %9.3f %s' % (rtt, id, server, start,
                    fmt_phase(phases['prep'], 4)), end='')
            print(' %s %s %s  %s' % (fmt_phase(phases['net']),
                    fmt_phase(phases['gro'], 5),
                    fmt_phase(phases['softirq'], 4),
                    fmt_phase(phases['handoff'])), end='')
            print(' %s %s %s %s' % (
                    fmt_phase(phases['queue']), fmt_phase(phases['sendmsg'], 4),
                    fmt_phase(phases['prep2'], 5), fmt_phase(phases['net2'])),
                    end='')
            print('  %s %s %s %s %s' % (fmt_phase(phases['gro2'], 5),
                    fmt_phase(phases['softirq2'], 5), fmt_phase(phases['handoff2']),
                    fmt_phase(phases['queue2'], 6), fmt_phase(phases['done'], 4)))
            to_print -= 1
            if to_print == 0:
                break

        # Print out phase averages for fast RPCs.
        fast_phases = []
        fast_rtt_sum = 0
        for i in range(len(rtts)):
            rtt, id, start, end, client, server = rtts[i]
            crpc = rpcs[id]
            server_id = id ^ 1
            if not server_id in rpcs:
                continue
            srpc = rpcs[server_id]
            fast_phases.append(get_phases(crpc, srpc))
            fast_rtt_sum += rtt
            if len(fast_phases) >= 10:
                break
        print('\nAverage times for the fastest short RPCs:')
        print('   RTT                                   Prep    Net   GRO SIRQ '
                'Handoff  Queue  App Prep2   Net2   GRO2 SIRQ2  Hand2 Queue2 Done')
        print('----------------------------------------------------------------'
                '----------------------------------------------------------------')
        print('%6.1f %33s %4.1f %6.1f %5.1f' % (
                fast_rtt_sum/len(fast_phases), '',
                dict_avg(fast_phases, 'prep'), dict_avg(fast_phases, 'net'),
                dict_avg(fast_phases, 'gro')), end='')
        print(' %4.1f %7.1f %6.1f %4.1f %5.1f' % (
                dict_avg(fast_phases, 'softirq'), dict_avg(fast_phases, 'handoff'),
                dict_avg(fast_phases, 'queue'), dict_avg(fast_phases, 'sendmsg'),
                dict_avg(fast_phases, 'prep2')), end='')
        print(' %6.1f %6.1f %5.1f %6.1f %6.1f %4.1f' % (
                dict_avg(fast_phases, 'net2'), dict_avg(fast_phases, 'gro2'),
                dict_avg(fast_phases, 'softirq2'), dict_avg(fast_phases, 'handoff2'),
                dict_avg(fast_phases, 'queue2'), dict_avg(fast_phases, 'done')))

        # Print out how much slower each phase is for slow RPCs than
        # for fast ones.
        print('\nAverage extra time spent by slow RPCs relative to fast ones:')
        print('   RTT                                   Prep    Net   GRO SIRQ '
                'Handoff  Queue  App Prep2   Net2   GRO2 SIRQ2  Hand2 Queue2 Done')
        print('----------------------------------------------------------------'
                '----------------------------------------------------------------')
        print('%6.1f %33s %4.1f %6.1f %5.1f' % (
                slow_rtt_sum/len(slow_phases) - fast_rtt_sum/len(fast_phases),
                '',
                dict_avg(slow_phases, 'prep') - dict_avg(fast_phases, 'prep'),
                dict_avg(slow_phases, 'net') - dict_avg(fast_phases, 'net'),
                dict_avg(slow_phases, 'gro') - dict_avg(fast_phases, 'gro')),
                        end='')
        print(' %4.1f %7.1f %6.1f %4.1f %5.1f' % (
                dict_avg(slow_phases, 'softirq') - dict_avg(fast_phases, 'softirq'),
                dict_avg(slow_phases, 'handoff') - dict_avg(fast_phases, 'handoff'),
                dict_avg(slow_phases, 'queue') - dict_avg(fast_phases, 'queue'),
                dict_avg(slow_phases, 'sendmsg') - dict_avg(fast_phases, 'sendmsg'),
                dict_avg(slow_phases, 'prep2') - dict_avg(fast_phases, 'prep2')),
                        end='')
        print(' %6.1f %6.1f %5.1f %6.1f %6.1f %4.1f' % (
                dict_avg(slow_phases, 'net2') - dict_avg(fast_phases, 'net2'),
                dict_avg(slow_phases, 'gro2') - dict_avg(fast_phases, 'gro2'),
                dict_avg(slow_phases, 'softirq2') - dict_avg(fast_phases, 'softirq2'),
                dict_avg(slow_phases, 'handoff2') - dict_avg(fast_phases, 'handoff2'),
                dict_avg(slow_phases, 'queue2') - dict_avg(fast_phases, 'queue2'),
                dict_avg(slow_phases, 'done') - dict_avg(fast_phases, 'done')))


#------------------------------------------------
# Analyzer: rx
#------------------------------------------------
class AnalyzeRx:
    """
    Generates one data file for each node showing various statistics
    related to incoming message reception as a function of time, including
    data rate, live messages, info about outstnanding grants, and where
    incoming data packets are curently located (qdisc, net, gro). Requires
    the --data and --gbps options.
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeIntervals')
        return

    def output(self):
        global intervals, options

        print('\n------------')
        print('Analyzer: rx')
        print('------------')
        if options.data == None:
            print('--data option wasn\'t specified, so no output generated.')
            return
        print('See data files rx_*.dat in %s\n' % (options.data))
        print('Average receive throughput:')

        for node in get_sorted_nodes():
            f = open('%s/rx_%s.dat' % (options.data, node), 'w')
            f.write('# Node: %s\n' % (node))
            f.write('# Generated at %s.\n' %
                    (time.strftime('%I:%M %p on %m/%d/%Y')))
            f.write('# Statistics about messages received by node ')
            f.write('%s over %d usec intervals:\n' % (node, options.interval))
            f.write('# Time:       End of the time interval\n')
            f.write('# Gbps:       Rate of data received by GRO during the interval\n')
            f.write('# Live:       Messages for which at least one packet has '
                    'been transmitted\n')
            f.write('#             by the peer, but which have not been fully '
                    'received by SoftIRQ,\n')
            f.write('#             as of the end of the interval\n')
            f.write('# Pkts:       Packets received by GRO during the interval\n')
            f.write('# Grantable:  # of incoming RPCs that are not fully '
                    'granted\n')
            f.write('# TxGrant:    KB of new grants passed to ip*xmit during '
                    'the interval\n')
            f.write('# Granted:    KB of grants that have been sent, but for '
                    'which corresponding\n')
            f.write('#             data packets have not been transmitted by '
                    'the peer\n')
            f.write('# IP:         KB of data that have been passed to ip*xmit '
                    'on sender but not\n')
            f.write('#             yet transmitted by NIC (large numbers '
                    'probably indicate qdisc\n')
            f.write('#             backup)\n')
            f.write('# Net:        KB of data that have been passed to the '
                    'NIC but not\n')
            f.write('#             yet received by GRO\n')
            f.write('# Late:       KB of data transmitted by NIC > %d us ago '
                    '(%d is the value\n' % (options.late, options.late))
            f.write('#             of the --late option) but not yet '
                    'seen by receiver\'s GRO\n')
            f.write('# GRO:        KB of data that have been received by '
                    'GRO but not yet\n')
            f.write('#             received by SoftIRQ\n')

            f.write('\n#   Time   Gbps  Live  Pkts Grantable TxGrant Granted'
                    '    IP   Net  Late   GRO\n')
            total = 0
            for interval in intervals[node]:
                if not 'rx_bytes' in interval:
                    print('Strange interval for %s: %s' % (node, interval))
                gbps = interval['rx_bytes'] * 8 / (options.interval * 1000)
                total += gbps
                f.write('%8.1f %6.1f %5d  %4d      %4d   %5.0f   %5.0f '
                        '%5.0f %5.0f %5.0f %5.0f\n'
                        % (interval['time'], gbps,
                        interval['rx_live'],
                        interval['rx_pkts'],
                        interval['rx_grantable'],
                        interval['rx_new_grants'] * 1e-3,
                        interval['rx_granted'] * 1e-3,
                        interval['rx_data_qdisc'] * 1e-3,
                        interval['rx_data_net'] * 1e-3,
                        interval['rx_overdue'] * 1e-3,
                        interval['rx_data_gro'] * 1e-3))
            f.close()
            print('%-10s %6.1f Gbps' % (node, total/len(intervals[node])))

#------------------------------------------------
# Analyzer: rxbufs
#------------------------------------------------
class AnalyzeRxbufs:
    """
    Analyzes lifetimes of skbs for incoming packets to compute total buffer
    usage for each channel and underflows of NIC buffer caches (based on
    caching mechanism of Mellanox mlx5 driver).
    """

    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')

    def output(self):
        global packets, rpcs

        # List of <time, type, id, core, length> records, where type is
        # "alloc" or "free", id is a packet id, core is the core where
        # homa_gro_receive processed the packet (in the form "node.core"),
        # and length is the number of bytes consumed by the packet.
        events = []

        # Core number (node.core) -> total number of bytes received so far
        # by homa_gro_receive on that core.
        core_bytes = defaultdict(lambda : 0)

        # Packet id -> <gro_time, core_bytes> tuple, where gro_time is the
        # time when the packet was processed by homa_gro_receive and
        # core_bytes is the value of core_bytes just before the packet
        # was allocated.
        pkt_allocs = {}

        # Core id -> <time, active_bytes, pkid, gro_time>, where active_bytes
        # is the largest number of active skb bytes seen for that core, time
        # is the time when some of those bytes were finally freed, pid is the
        # id of the packet freed at time, and gro_time is the time when that
        # packet was processed by homa_gro_receive.
        core_max = defaultdict(lambda : [0, 0, '', 0])

        # Scan all packets to build the events list. Note: change packet
        # ids to refer to those on the receiver, not sender.
        for pkt in packets.values():
            if not 'gro' in pkt:
                continue
            rpc_id = pkt['id'] ^ 1
            pkid = '%d:%d' % (rpc_id, pkt['offset'])
            rpc = rpcs[rpc_id]
            core = '%s.%d' % (rpc['node'], rpc['gro_core'])
            events.append([pkt['gro'], 'alloc', pkid, core, pkt['length']])
            if 'free' in pkt:
                events.append([pkt['free'], 'free', pkid, core, pkt['length']])

        # Process the events in time order
        events.sort(key=lambda t : t[0])
        for time, type, pkid, core, length in events:
            if type == 'alloc':
                pkt_allocs[pkid] = [time, core_bytes[core]]
                core_bytes[core] += length
            elif type == 'free':
                if pkid in pkt_allocs:
                    active_bytes = core_bytes[core] - pkt_allocs[pkid][1]
                    if active_bytes > core_max[core][1]:
                        core_max[core] = [time, active_bytes, pkid,
                                pkt_allocs[pkid][0]]
            else:
                print('Bogus event type %s in nicbufs analzyer' % (type),
                        file=sys.stderr)


        print('\n-----------------')
        print('Analyzer: nicbufs')
        print('-----------------')
        print('Maximum active NIC buffer space used for each GRO core over the')
        print('life of the traces (assuming Mellanox mlx5 buffer cache):')
        print('Active:    Maximum bytes of NIC buffers used by the core (bytes')
        print('           allocated on Core between when PktId was received and')
        print('           when PktId was freed)')
        print('PktId:     Identifier (as seen by receiver) for the packet ')
        print('           corresponding to Active')
        print('Node:      Node where Pktid was received')
        print('Core:      Core on which Pktid was received')
        print('GRO:       Time when homa_gro_receive processed Pktid on Core')
        print('Free:      Time when packet was freed after copying to user space')
        print('Life:      Packet lifetime (Free - GRO, usecs)\n')

        maxes = []
        for core, max in core_max.items():
            time, active, pkid, gro_time = max
            maxes.append([core, time, active, pkid, gro_time])
        maxes.sort(key=lambda t : t[2], reverse = True)
        print('  Active                PktId       Node Core       GRO      '
                'Free    Life')
        print('-------------------------------------------------------------'
                '------------')
        for core, time, active, pkid, gro_time in maxes:
            node, core_id = core.split('.')
            print('%8d %20s %10s %4s %9.3f %9.3f %7.1f' % (active, pkid,
                    node, core_id, gro_time, time, time - gro_time))

#------------------------------------------------
# Analyzer: smis
#------------------------------------------------
class AnalyzeSmis:
    """
    Prints out information about SMIs (System Management Interrupts) that
    occurred during the traces. An SMI causes all of the cores on a node
    to freeze for a significant amount of time.
    """
    def __init__(self, dispatcher):
        # A list of <start, end, node> tuples, each of which describes one
        # gap that looks like an SMI.
        self.smis = []

        # Time of the last trace record seen.
        self.last_time = None
        return

    def tt_all(self, trace, t, core, msg):
        if self.last_time == None:
            self.last_time = t
            return
        if (t - self.last_time) > 50:
            self.smis.append([self.last_time, t, trace['node']])
        self.last_time = t

    def output(self):
        print('\n-------------------')
        print('Analyzer: smis')
        print('-------------------')
        print('Gaps that appear to be caused by System Management '
                'Interrupts (SMIs),')
        print('which freeze all cores on a node simultaneously:')
        print('')
        print('    Start        End     Gap   Node')
        print('-----------------------------------')
        for smi in sorted(self.smis, key=lambda t : t[0]):
            start, end, node = smi
            print('%9.3f  %9.3f  %6.1f  %s' % (start, end, end - start, node))

#------------------------------------------------
# Analyzer: snapshot
#------------------------------------------------
class AnalyzeSnapshot:
    """
    Prints information about the state of incoming messages to a particular
    node at a given time. Requires the --node and --time options.
    """

    def __init__(self, dispatcher):
        global options
        require_options('snapshot', 'time', 'node')
        dispatcher.interest('AnalyzeRpcs')
        dispatcher.interest('AnalyzePackets')

    def analyze(self):
        global packets, grants, rpcs, options, traces

        # RPC id -> dictionary (for all RPCs with live incoming messages
        # at the target time):
        # pkts:              List of all the data packets in this RPC
        # grants:            List of all the grant packets in this RPC
        # pre_xmit:          Offset just after highest byte sent in a data
        #                    packet with 'xmit' < target time
        # post_xmit:         Lowest offset contained ina a data packet with
        #                    'xmit' >= target time
        # pre_gro and post_gro:
        #                    Same, except measured with 'gro' instead of 'xmit'
        # pre_softirq and post_softirq:
        #                    Same, except measured with 'softirq' instead of 'xmit'
        # pre_copied and post_copied:
        #                    Same, except measured with 'copied' instead of 'xmit'
        #
        # The following offsets record things that happened either before
        # or after the target time.
        # pre_grant_xmit:    Highest end offset seen in a grant with 'xmit'
        #                    < target time
        # post_grant_xmit:   Lowest (starting) offset seen in a grant with
        #                    'xmit' >= target time
        # pre_grant_gro and post_grant_gro:
        #                    Same, except measured with 'gro' instead of 'xmit'
        # pre_grant_softirq and post_grant_softirq:
        #                    Same, except measured with 'softirq' instead
        #                    of 'xmit'
        # min_time:          Lowest "interesting" time seen in any packet
        #                    for this RPC
        # lost:              Number of packets that appear to have been lost
        #                    (transmitted but not received after long delay)
        self.live_rpcs = defaultdict(lambda : {'pkts': [], 'grants': [],
                'pre_xmit': 0, 'post_xmit': 1e20,
                'pre_gro': 0, 'post_gro': 1e20,
                'pre_softirq': 0, 'post_softirq': 1e20,
                'pre_copied': 0, 'post_copied': 1e20,
                'pre_grant_xmit': 0, 'post_grant_xmit': 1e20,
                'pre_grant_gro': 0, 'post_grant_gro': 1e20,
                'pre_grant_softirq': 0, 'post_grant_softirq': 1e20,
                'lost': 0, 'min_time': 1e20
        })

        t = options.time
        node = options.node
        trace_start = traces[node]['first_time']

        # Collect info from data packets
        for pkt in packets.values():
            id = pkt['id']^1
            if not id in rpcs:
                continue
            rpc = rpcs[id]
            if rpc['node'] != node:
                continue
            if not 'rx_live' in rpc:
                continue
            start, end = rpc['rx_live']
            if (start > t) or (end <= t):
                continue

            live_rpc = self.live_rpcs[id]
            live_rpc['pkts'].append(pkt)

            offset = pkt['offset']
            end_offset = offset + pkt['length']
            for type in ['xmit', 'gro', 'softirq', 'copied']:
                if (type in pkt):
                    pkt_time = pkt[type]
                    if pkt_time < t:
                        if end_offset > live_rpc['pre_' + type]:
                            live_rpc['pre_' + type] = end_offset
                    else:
                        if offset < live_rpc['post_' + type]:
                            live_rpc['post_' + type] = offset

        # Collect info from grant packets
        for pkt in grants.values():
            id = pkt['id']
            if not id in rpcs:
                continue
            rpc = rpcs[id]
            if rpc['node'] != node:
                continue
            if not 'rx_live' in rpc:
                continue
            start, end = rpc['rx_live']
            if (start > t) or (end <= t):
                continue
            live_rpc = self.live_rpcs[id]
            live_rpc['grants'].append(pkt)

            end_offset = pkt['offset']
            offset = end_offset - pkt['increment']
            for type in ['xmit', 'gro', 'softirq']:
                if (type in pkt):
                    pkt_time = pkt[type]
                    if pkt_time < t:
                        if end_offset > live_rpc['pre_grant_' + type]:
                            live_rpc['pre_grant_' + type] = end_offset
                    else:
                        if offset < live_rpc['post_grant_' + type]:
                            live_rpc['post_grant_' + type] = offset


        # Deduce missing fields in RPCs where possible
        for id, live_rpc in self.live_rpcs.items():
            next_stage = 0
            for type in ['copied', 'softirq', 'gro', 'xmit']:
                pre_field = 'pre_' + type
                post_field = 'post_' + type
                pre = live_rpc[pre_field]
                post = live_rpc[post_field]
                if pre == 0:
                    # There were no packets with times before the target;
                    # However, we can infer the field value from the first
                    # packet after the target time, if any, or from information
                    # from the next later stage (e.g. if a byte got to SoftIRQ
                    # it must have been received by GRO).
                    if post < 1e20:
                        live_rpc[pre_field] = post
                    elif next_stage != 0:
                        live_rpc[pre_field] = next_stage
                next_stage = pre

            next_stage = 0
            rpc = rpcs[id]
            unsched = rpc['unsched'] if 'unsched' in rpc else 0
            for type in ['softirq', 'gro', 'xmit']:
                pre_field = 'pre_grant_' + type
                post_field = 'post_grant_' + type
                pre = live_rpc[pre_field]
                post = live_rpc[post_field]
                if pre == 0:
                    if post < 1e20:
                        live_rpc[pre_field] = post
                    elif next_stage != 0:
                        live_rpc[pre_field] = next_stage
                    elif (type == 'softirq') and (unsched > 0) and (
                            live_rpc['pre_xmit'] > unsched):
                        live_rpc[pre_field] = live_rpc['pre_xmit']
                next_stage = pre
                if live_rpc[pre_field] <= unsched:
                    live_rpc[pre_field] = 0

            # Count lost packets in the RPC.
            for pkt in live_rpc['pkts']:
                if (('xmit' in pkt) and (not 'gro' in pkt)
                        and ((options.time - pkt['xmit']) > 200)):
                    live_rpc['lost'] += 1

    def output(self):
        global packets, rpcs, options, traces

        print('\n-------------------')
        print('Analyzer: snapshot')
        print('-------------------')
        print('A snapshot of the state of %s at time %.1f' % (options.node,
                options.time))

        print('\n%d RPCs have live incoming messages:' %
                (len(self.live_rpcs)))
        print('Id:        RPC identifier on the receiver side')
        print('Length:    Length of incoming message, if known')
        print('Gxmit:     Highest offset for which grant has been passed '
                'to ip_*xmit')
        print('GGro:      Highest offset in grant that has been received by GRO')
        print('GSoft:     Highest offset in grant that has been processed '
                'by SoftIRQ')
        print('Xmit:      Offset just after last data byte that has been '
                'passed to ip*xmit')
        print('Gro:       Offset just after last data byte that has been '
                'processed by GRO')
        print('SoftIrq:   Offset just after last data byte that has been '
                'processed by SoftIRQ')
        print('Copied:    Offset just after last data byte that has been '
                'copied to user space')
        print('Incoming:  Gxmit - SoftIrq')
        print('Lost:      Packets that appear to have been dropped in the network')
        print('        Id  Length   GXmit    GGro   GSoft ', end='')
        print('   Xmit     Gro SoftIrq  Copied Incoming Lost')
        print('-------------------------------------------', end='')
        print('---------------------------------------------')

        sorted_ids = sorted(self.live_rpcs.keys(),
                key = lambda id2 : self.live_rpcs[id2]['min_time'])

        # Separate out messsage that have been granted from those that have
        # never been given a grant.
        granted = ''
        ungranted = ''
        for id in sorted_ids:
            rpc = rpcs[id]
            live_rpc = self.live_rpcs[id]
            incoming = (live_rpc['pre_grant_xmit'] - live_rpc['pre_softirq']
                    if live_rpc['pre_grant_xmit'] > 0 else 0)
            line = '%10d %7s %7s %7s %7s ' % (id,
                    rpc['in_length'] if rpc['in_length'] != None else "",
                    str(live_rpc['pre_grant_xmit'])
                    if live_rpc['pre_grant_xmit'] > 0 else "",
                    str(live_rpc['pre_grant_gro'])
                    if live_rpc['pre_grant_gro'] > 0 else "",
                    str(live_rpc['pre_grant_softirq'])
                    if live_rpc['pre_grant_softirq'] > 0 else "")
            line += '%7d %7d %7d %7d  %7d %4d\n' % (live_rpc['pre_xmit'],
                    live_rpc['pre_gro'], live_rpc['pre_softirq'],
                    live_rpc['pre_copied'], incoming, live_rpc['lost'])
            if live_rpc['pre_grant_xmit'] > 0:
                granted += line
            else:
                ungranted += line
        print(granted, end='')
        print(ungranted, end='')

        print('\nFields in the tables below:')
        print('Id:        Packet\'s RPC identifier on the receiver side')
        print('Offset:    Starting offset of packet data within its message')
        print('TxCore:    Core where sender passed packet to ip*xmit')
        print('GCore:     Core where receiver GRO processed packet')
        print('SCore:     Core where receiver SoftIRQ processed packet')
        print('Xmit:      Time when sender passed packet to ip*xmit')
        print('Gro:       Time when receiver GRO processed packet')
        print('SoftIrq:   Time when receiver SoftIRQ processed packet')
        print('Numbers in parentheses give the difference between the '
                'preceding value')
        print('and the reference time')

        for id in sorted_ids:
            live_rpc = self.live_rpcs[id]
            rpc = rpcs[id]
            info = ''
            prefix = ' ('
            if rpc['in_length'] != None:
                info += '%s%d bytes' % (prefix, rpc['in_length'])
                prefix = ', '
            received = get_received(rpc, options.time)
            if received != None:
                info += '%sreceived %d' % (prefix, received)
                prefix = ', '
            granted = get_granted(rpc, options.time)
            if granted != None:
                info += '%sgranted %d' % (prefix, granted)
                prefix = ', '
            if 'peer' in rpc:
                info += '%speer %s' % (prefix, peer_nodes[rpc['peer']])
            if info:
                info += ')'

            live_rpc['pkts'].sort(key = lambda d : d['offset'])
            net_pkts = []
            gro_pkts = []
            for pkt in live_rpc['pkts']:
                offset = pkt['offset']
                keep = True
                if 'xmit' in pkt:
                    if pkt['xmit'] >= options.time:
                        keep = False
                elif offset >= live_rpc['pre_xmit']:
                        keep = False
                if 'gro' in pkt:
                    if pkt['gro'] < options.time:
                        keep = False
                elif offset < live_rpc['pre_gro']:
                    keep = False
                if keep:
                    net_pkts.append(pkt)

                keep = False
                if 'gro' in pkt:
                    if pkt['gro'] >= options.time:
                        keep = False
                elif offset >= live_rpc['pre_gro']:
                        keep = False
                if 'softirq' in pkt:
                    if pkt['softirq'] < options.time:
                        keep = False
                elif offset < live_rpc['pre_softirq']:
                    keep = False
                if keep:
                    gro_pkts.append(pkt)

            live_rpc['grants'].sort(key = lambda d : d['offset'])
            net_grants = []
            gro_grants = []
            for pkt in live_rpc['grants']:
                offset = pkt['offset']
                keep = True
                if 'xmit' in pkt:
                    if pkt['xmit'] > options.time:
                        keep = False
                elif offset > live_rpc['pre_xmit']:
                        keep = False
                if 'gro' in pkt:
                    if pkt['gro'] <= options.time:
                        keep = False
                elif offset <= live_rpc['pre_gro']:
                    keep = False
                if keep:
                    net_grants.append(pkt)

                keep = False
                if 'gro' in pkt:
                    if pkt['gro'] > options.time:
                        keep = False
                elif offset > live_rpc['pre_gro']:
                        keep = False
                if 'softirq' in pkt:
                    if pkt['softirq'] <= options.time:
                        keep = False
                elif offset <= live_rpc['pre_softirq']:
                    keep = False
                if keep:
                    gro_grants.append(pkt)

            if (not net_pkts) and (not gro_pkts) and (not net_grants) and (
                    not gro_grants):
                continue
            print('\nRPC id %d%s:' % (id, info))

            if net_pkts:
                print('Incoming data packets that have been transmitted but '
                        'not received by GRO:')
                print('Offset      Xmit           TxCore       Gro        GCore')
                for pkt in net_pkts:
                    print('%6d %7s   %-10s  %4s %7s %8s %5s' % (
                            pkt['offset'], print_field_if(pkt, 'xmit', '%7.1f'),
                            print_field_if(pkt, 'xmit', '(%7.1f)',
                                     lambda t : t - options.time ),
                            print_field_if(pkt, 'tx_core', '%4d'),
                            print_field_if(pkt, 'gro', '%7.1f'),
                            print_field_if(pkt, 'gro', '(%6.1f)',
                                     lambda t : t - options.time),
                            print_field_if(pkt, 'gro_core', '%3d')))

            if gro_pkts:
                print('Incoming data packets that have been seen by GRO but '
                        'not yet by SoftIRQ:')
                print('Offset        Gro         GCore     SoftIRQ        SCore')
                for pkt in gro_pkts:
                    print('%6d  %7s %9s %5s %7s %8s %7s' % (
                            pkt['offset'], print_field_if(pkt, 'gro', '%7.1f'),
                            print_field_if(pkt, 'gro', '(%7.1f)',
                                     lambda t : t - options.time),
                            print_field_if(pkt, 'gro_core', '%3d'),
                            print_field_if(pkt, 'softirq', '%7.1f'),
                            print_field_if(pkt, 'softirq', '(%6.1f)',
                                     lambda t : t - options.time),
                            print_field_if(pkt, 'softirq_core', '%3d')))

            if net_grants:
                print('Outgoing grants that have been passed to ip*xmit but '
                        'not received by GRO:')
                print('Offset      Xmit           TxCore       Gro        GCore')
                for pkt in net_grants:
                    print('%6d %7s   %-10s  %4s %7s %8s %5s' % (
                            pkt['offset'], print_field_if(pkt, 'xmit', '%7.1f'),
                            print_field_if(pkt, 'xmit', '(%7.1f)',
                                     lambda t : t - options.time ),
                            print_field_if(pkt, 'tx_core', '%4d'),
                            print_field_if(pkt, 'gro', '%7.1f'),
                            print_field_if(pkt, 'gro', '(%6.1f)',
                                     lambda t : t - options.time),
                            print_field_if(pkt, 'gro_core', '%3d')))
            if gro_grants:
                print('Outgoing grants that have been seen by GRO but not '
                        'yet by SoftIRQ:')
                print('Offset        Gro         GCore     SoftIRQ        SCore')
                for pkt in gro_grants:
                    print('%6d  %7s %9s %5s %7s %8s %7s' % (
                            pkt['offset'], print_field_if(pkt, 'gro', '%7.1f'),
                            print_field_if(pkt, 'gro', '(%7.1f)',
                                     lambda t : t - options.time),
                            print_field_if(pkt, 'gro_core', '%3d'),
                            print_field_if(pkt, 'softirq', '%7.1f'),
                            print_field_if(pkt, 'softirq', '(%6.1f)',
                                     lambda t : t - options.time),
                            print_field_if(pkt, 'softirq_core', '%3d')))

#------------------------------------------------
# Analyzer: temp
#------------------------------------------------
class AnalyzeTemp:
    """
    This analyzer is used to implement temporary checks used during
    debugging. Consult the code to see what it does right now.
    """
    def __init__(self, dispatcher):
        if True:
            dispatcher.interest('AnalyzeRpcs')
            dispatcher.interest('AnalyzePackets')

    def output(self):
        global traces, options, packets
        print('\n-------------------')
        print('Analyzer: temp')
        print('-------------------')

        pkts = []
        node3pkts = 0
        long = 50
        node = options.node
        for pkt in packets.values():
            if not 'nic' in pkt:
                continue
            if not 'free_tx_skb' in pkt:
                continue
            if not 'gro' in pkt:
                continue
            max_gro = get_max_gro(pkt)
            if (node != None) and ('tx_node' in pkt) and (node != pkt['tx_node']):
                continue
            node3pkts += 1
            delta = pkt['free_tx_skb'] - max_gro
            if delta < long:
                continue
            pkts.append([delta, pkt])
        if pkts:
            print('%d/%d packets (%.1f%%) had free delays > %d usec'% (
                    len(pkts), node3pkts, 100*len(pkts)/node3pkts, long))
        else:
            print('No packets had long free delays')
        for delay, pkt in sorted(pkts, reverse=True, key=lambda t : t[0]):
            print('RPC id %10d (%s), offset %6d, nic %9.3f, max_gro %9.3f, free %9.3f, '
                    'free delay %7.3f' % (pkt['id'], pkt['tx_node'], pkt['offset'],
                    pkt['nic'], get_max_gro(pkt), pkt['free_tx_skb'],
                    pkt['free_tx_skb'] - pkt['gro']))

    def output_long_qdisc(self):
        global traces, options, packets
        print('\n-------------------')
        print('Analyzer: temp')
        print('-------------------')

        pkts = []
        for pkt_id in self.qdisc_ids:
            if not pkt_id in packets:
                continue
            pkt = packets[pkt_id]
            if (not 'xmit' in pkt) or (not 'gro' in pkt):
                continue
            if not 'nic' in pkt:
                print('Queued packet has no mlx send record: %s' % (pkt))
                continue
            pkts.append([pkt['nic'] - pkt['xmit'], pkt])

        for delay, pkt in sorted(pkts, reverse=True, key=lambda t : t[0]):
            print('RPC id %10d, offset %d, xmit %9.3f, mlx_delay %6.1f, '
                    'gro_delay %6.1f' % (pkt['id'], pkt['offset'],
                    pkt['xmit'], delay, pkt['gro'] - pkt['xmit']))

    def output_snapshot(self):
        global packets, rpcs

        # Desired time for snapshot
        t = 18000.0

        # Desired target node
        target = 'node3'

        print('\n-------------------')
        print('Analyzer: temp')
        print('-------------------')
        print('Packets incoming to %s at time %.1f' % (target, t))

        # Node name -> {pkts, bytes} in transit from node at given time.
        nodes = {}

        # Core number -> {pkts, bytes} in transit to GRO core at given time.
        cores = {}

        # RPC id -> {pkts, bytes} in transit for that RPC at given time.
        rpc_counts = {}

        total_packets = 0
        total_bytes = 0

        for pkt in packets.values():
            if False:
                print('Packet: %s' % (pkt))
            missing_fields = False
            for field in ['xmit', 'gro', 'id', 'gro_core', 'offset']:
                if not field in pkt:
                    missing_fields = True
                    break
            if missing_fields:
                continue
            if pkt['xmit'] > t:
                continue
            if pkt['gro'] < t:
                continue
            id = pkt['id']
            tx_node = rpcs[id]['node']
            rx_node = rpcs[id^1]['node']
            if rx_node != target:
                continue
            length = get_recv_length(pkt['offset'], pkt['msg_length'])
            total_packets += 1
            total_bytes += length

            if not tx_node in nodes:
                nodes[tx_node] = {'pkts': 0, 'bytes': 0}
            node = nodes[tx_node]
            node['pkts'] += 1
            node['bytes'] += length

            if not pkt['gro_core'] in cores:
                cores[pkt['gro_core']] = {'pkts': 0, 'bytes': 0}
            core = cores[pkt['gro_core']]
            core['pkts'] += 1
            core['bytes'] += length

            if not id in rpc_counts:
                rpc_counts[id] = {'pkts': 0, 'bytes': 0}
            rpc = rpc_counts[id]
            rpc['pkts'] += 1
            rpc['bytes'] += length

        print('\nTotal packets %d, total bytes %d' % (total_packets, total_bytes))

        print('\nSource nodes:')
        print('Node        Pkts    Bytes')
        for name in get_sorted_nodes():
            if not name in nodes:
                continue
            node = nodes[name]
            print('%-10s %5d %8d' % (name, node['pkts'], node['bytes']))


        print('\nGRO cores:')
        print('Node   Pkts    Bytes')
        for core_num in sorted(cores.keys()):
            core = cores[core_num]
            print('%4d  %5d %8d' % (core_num, core['pkts'], core['bytes']))

        print('\nRPCs:')
        print('Id           Pkts    Bytes')
        for id in sorted(rpc_counts.keys()):
            rpc = rpc_counts[id]
            print('%-10d  %5d %8d' % (id, rpc['pkts'], rpc['bytes']))

#------------------------------------------------
# Analyzer: timeline
#------------------------------------------------
class AnalyzeTimeline:
    """
    Prints a timeline showing how long it takes for RPCs to reach various
    interesting stages on both clients and servers. Most useful for
    benchmarks where all RPCs are the same size.
    """
    def __init__(self, dispatcher):
        dispatcher.interest('AnalyzeRpcs')
        return

    def output(self):
        global rpcs
        num_rpcs = 0
        print('\n-------------------')
        print('Analyzer: timeline')
        print('-------------------')

        # These tables describe the phases of interest. Each sublist is
        # a <label, name, lambda> triple, where the label is human-readable
        # string for the phase, the name selects an element of an RPC, and
        # the lambda extracts a time from the RPC element.
        client_phases = [
            ['first request packet sent',     'send_data',    lambda x : x[0][0]],
            ['softirq gets first grant',      'softirq_grant',lambda x : x[0][0]],
            ['last request packet sent',      'send_data',    lambda x : x[-1][0]],
            ['gro gets first response packet','gro_data',     lambda x : x[0][0]],
            ['softrq gets first response pkt','softirq_data', lambda x : x[0][0]],
            ['sent grant',                    'send_grant',   lambda x : x[0][0]],
            ['gro gets last response packet', 'gro_data',     lambda x : x[-1][0]],
            ['homa_recvmsg returning',        'recvmsg_done', lambda x : x]
            ]
        client_extra = [
            ['finished copying req into pkts','copy_in_done',  lambda x : x],
            ['started copying to user space', 'copy_out_start',lambda x : x],
            ['finished copying to user space','copy_out_done', lambda x : x]
        ]

        server_phases = [
            ['gro gets first request packet',  'gro_data',      lambda x : x[0][0]],
            ['softirq gets first request pkt', 'softirq_data',  lambda x : x[0][0]],
            ['sent grant',                     'send_grant',    lambda x : x[0][0]],
            ['gro gets last request packet',   'gro_data',       lambda x : x[-1][0]],
            ['homa_recvmsg returning',         'recvmsg_done',  lambda x : x],
            ['homa_sendmsg response',          'sendmsg',       lambda x : x],
            ['first response packet sent',     'send_data',     lambda x : x[0][0]],
            ['softirq gets first grant',       'softirq_grant', lambda x : x[0][0]],
            ['last response packet sent',      'send_data',     lambda x : x[-1][0]]
        ]
        server_extra = [
            ['started copying to user space', 'copy_out_start', lambda x : x],
            ['finished copying to user space','copy_out_done',  lambda x : x],
            ['finished copying req into pkts','copy_in_done',   lambda x : x]
        ]

        # One entry in each of these lists for each phase of the RPC,
        # values are lists of times from RPC start (or previous phase)
        client_totals = []
        client_deltas = []
        client_extra_totals = []
        client_extra_deltas = []
        server_totals = []
        server_deltas = []
        server_extra_totals = []
        server_extra_deltas = []

        # Collect statistics from all of the RPCs.
        for id, crpc in rpcs.items():
            # Find matching and complete pairs of client-side and
            # serve-side RPCs.
            if id & 1:
                continue
            if not ((id^1) in rpcs):
                continue
            srpc = rpcs[id^1]
            if (not 'sendmsg' in crpc) or (not 'recvmsg_done' in crpc):
                continue
            if (not crpc['gro_data']) or (crpc['gro_data'][0][1] != 0) \
                    or (not crpc['send_data']):
                continue
            num_rpcs += 1

            start = crpc['sendmsg']
            self.__collect_stats(client_phases, crpc, start, client_totals,
                    client_deltas)
            self.__collect_stats(client_extra, crpc, start, client_extra_totals,
                    client_extra_deltas)
            self.__collect_stats(server_phases, srpc, start, server_totals,
                    server_deltas)
            self.__collect_stats(server_extra, srpc, start, server_extra_totals,
                    server_extra_deltas)

        if client_totals:
            print('\nTimeline for clients (%d RPCs):\n' % (num_rpcs))
            self.__print_phases(client_phases, client_totals, client_deltas)
            print('')
            self.__print_phases(client_extra, client_extra_totals,
                    client_extra_deltas)
        if server_totals:
            print('\nTimeline for servers (%d RPCs):\n' % (num_rpcs))
            self.__print_phases(server_phases, server_totals, server_deltas)
            print('')
            self.__print_phases(server_extra, server_extra_totals,
                    server_extra_deltas)

    def __collect_stats(self, phases, rpc, start, totals, deltas):
        """
        Utility method used by print to aggregate delays within an RPC
        into buckets corresponding to different phases of the RPC.
        phases:     Describes the phases to aggregate
        rpc:        Dictionary containing information about one RPC
        start:      Starting time for RPC on client-side
        totals:     Total delays from start of the RPC are collected here
        deltas:     Delays from one phase to the next are collected here
        """

        while len(phases) > len(totals):
            totals.append([])
            deltas.append([])
        prev = start
        for i in range(len(phases)):
            phase = phases[i]
            if phase[1] in rpc:
                rpc_phase = rpc[phase[1]]
                if rpc_phase:
                    t = phase[2](rpc_phase)
                    totals[i].append(t - start)
                    deltas[i].append(t - prev)
                    prev = t

    def __print_phases(self, phases, totals, deltas):
        """
        Utility method used by print to print out summary statistics
        aggregated by __phase_stats
        """
        for i in range(0, len(phases)):
            label = phases[i][0]
            if not totals[i]:
                print('%-32s (no events)' % (label))
                continue
            elapsed = sorted(totals[i])
            gaps = sorted(deltas[i])
            print('%-32s Avg %7.1f us (+%7.1f us)  P90 %7.1f us (+%7.1f us)' %
                (label, sum(elapsed)/len(elapsed), sum(gaps)/len(gaps),
                elapsed[9*len(elapsed)//10], gaps[9*len(gaps)//10]))

#------------------------------------------------
# Analyzer: txintervals
#------------------------------------------------
class AnalyzeTxintervals:
    """
    Computes statistics related to packet transmission over intervals,
    and generates one data file for each node showing interval data for
    that node. Requires the --data and --gbps options. Also uses the
    --interval option.  If --tx-qid is specified, then only packets transmitted
    via that qid will be considered.
    """

    def __init__(self, dispatcher):
        interval_analyzer = dispatcher.interest('AnalyzeIntervals')
        if options.tx_qid != None:
            interval_analyzer.restrict_qid(options.tx_qid)
        return

    def output(self):
        global intervals, options, traces

        print('\n------------')
        print('Analyzer: tx')
        print('------------')
        if options.data == None:
            print('--data option wasn\'t specified, so no output generated.')
            return
        print('See data files tx_*.dat in %s\n' % (options.data))
        print('Average transmit throughput:')

        if options.tx_qid != None:
            qid_msg = ' (considers only transmit queue %d)' % (options.tx_qid)
        else:
            qid_msg = ''

        for node in get_sorted_nodes():
            f = open('%s/txintervals_%s.dat' % (options.data, node), 'w')
            f.write('# Node: %s\n' % (node))
            f.write('# Generated at %s.\n' %
                    (time.strftime('%I:%M %p on %m/%d/%Y')))
            f.write('# Statistics about message transmission from node ')
            f.write('%s over %d usec\n' % (node, options.interval))
            f.write('# intervals%s:\n' % (qid_msg))
            f.write('# Time:       End of the time interval\n')
            f.write('# Gbps:       Rate of data passed to ip*xmit during '
                    'the interval\n')
            f.write('# TxKB:       KB of data passed to ip*xmit during '
                    'the interval\n')
            f.write('# RPCs:       Number of live client RPCs at the end '
                    'of the interval\n')
            f.write('# Reqs:       Request messages that have been started '
                    'but not fully\n')
            f.write('              transmitted as of the end of the interval\n')
            f.write('# Resps:      Response messages that have been started '
                    'but not fully\n')
            f.write('              transmitted as of the end of the interval\n')
            f.write('# Pkts:       Packets transmitted during the interval\n')
            f.write('# QDisc:      KB of data that have been passed to ip*xmit '
                    'but not yet\n')
            f.write('#             transmitted by NIC, as of the end of the '
                    'interval\n')
            f.write('# NicKB:      KB of data passed to NIC during the interval\n')
            f.write('# NQEst:      Estimate of NIC queue length at the end '
                    'of the interval,\n')
            f.write('#             measured in usecs to xmit (assumes the NIC '
                    'can transmit at\n')
            f.write('              link speed)\n')
            f.write('# InNic:      KB of data that have been queued for the '
                    'NIC but whose packets\n')
            f.write('#             haven\'t been returned after transmission, '
                    'as of the end of\n')
            f.write('#             the interval\n')
            f.write('# FreeKB:     KB of skb data freed after NIC notified '
                    'transmission complete\n')
            f.write('# MinFr:      Smallest p[\'free_tx_skb\'] - p[\'nic\'] for a '
                    'packet freed in\n')
            f.write('#             this interval\n')
            f.write('# MaxFr:      Largest p[\'free_tx_skb\'] - p[\'nic\'] for a '
                    'packet freed in\n')
            f.write('#             this interval\n')
            f.write('# MinGF:      Smallest p[\'gro\'] - p[\'free_tx_skb\'] '
                    'for any segment of\n')
            f.write('#             a packet freed in this interval\n')
            f.write('# MaxGF:      Largest p[\'gro\'] - p[\'free_tx_skb\'] '
                    'for any segment of\n')
            f.write('#             a packet freed in this interval\n')
            f.write('# GXmit:      KB of grants that have been sent by peer '
                    'but not yet\n')
            f.write('              received by GRO\n')
            f.write('# GGro:       KB of grants that have been received by GRO '
                    'but not yet\n')
            f.write('              received by SoftIRQ\n')
            f.write('# GAvail:     KB of grants that have been received by '
                    'SoftIRQ but data hasn\'t\n')
            f.write('              been transmitted yet\n')
            f.write('# GNew:       KB of new grants received by SoftIRQ '
                    'during the interval\n')

            f.write('\n#   Time   Gbps  TxKB  RPCs   Reqs  Resps')
            f.write(' Pkts Qdisc NicKB NQEst InNic FreeKB')
            f.write('  MinFr  MaxFr  MinGF  MaxGF')
            f.write(' GXmit  GGro GAvail   GNew\n')
            total = 0
            for interval in intervals[node]:
                if not 'tx_bytes' in interval:
                    print('Bogus interval: %s' % (interval))
                    print('Trace: %s' % (traces[node]))
                gbps = interval['tx_bytes'] * 8 / (options.interval * 1000)
                total += gbps
                f.write('%8.1f %6.1f %5.0f %5d  %5d  %5d' %
                        (interval['time'], gbps,
                        interval['tx_bytes'] * 1e-3,
                        interval['rpcs_live'],
                        interval['tx_live_req'],
                        interval['tx_live_resp']))
                f.write(' %4d %5.0f %5.0f %5.1f %5.0f  %5.0f' % (
                        interval['tx_pkts'], interval['tx_qdisc'] * 1e-3,
                        interval['tx_nic_bytes'] * 1e-3,
                        interval['tx_q'] * 8 / (options.gbps * 1000),
                        interval['tx_in_nic'] * 1e-3,
                        interval['tx_free_bytes'] * 1e-3))
                v = interval['tx_min_free']
                min_free = '%.1f' % v if v != 0 else ''
                v = interval['tx_max_free']
                max_free = '%.1f' % v if v != 0 else ''
                v = interval['tx_min_gro_free']
                min_gro_free= '%.1f' % v if v != None else ''
                v = interval['tx_max_gro_free']
                max_gro_free = '%.1f' % v if v != None else ''
                f.write(' %6s %6s %6s %6s' % (min_free, max_free, min_gro_free,
                        max_gro_free))
                f.write(' %5.0f %5.0f  %5.0f %5.0f\n'
                        % (interval['tx_grant_xmit'] * 1e-3,
                        interval['tx_grant_gro'] * 1e-3,
                        interval['tx_grant_avl'] * 1e-3,
                        interval['tx_new_grants'] * 1e-3))
            f.close()
            print('%-10s %6.1f Gbps' % (node, total/len(intervals[node])))

#------------------------------------------------
# Analyzer: txpkts
#------------------------------------------------
class AnalyzeTxpkts:
    """
    Generates one data file for each node showing information about every
    data packet transmitted from that node, in time order. Also generates
    aggregate delay statistics by node and core. If either --node or --tx-qid
    is specified, only packets matching those options will be considered.
    """

    def __init__(self, dispatcher):
        global options
        require_options('txpkts', 'data')
        dispatcher.interest('AnalyzePackets')

    def output(self):
        global packets, options

        # node -> list of packets transmitted by that node
        node_pkts = defaultdict(list)

        # Bucket all of the packets by transmitting node.
        for pkt in packets.values():
            if (not 'xmit' in pkt) or not ('tso_length' in pkt):
                continue
            if (not 'nic' in pkt) or (not 'gro' in pkt) or (not 'tx_qid' in pkt):
                continue
            node_pkts[pkt['tx_node']].append(pkt)

        print('\n----------------')
        print('Analyzer: txpkts')
        print('----------------')
        print('See data files txpkts_*.dat in %s\n' % (options.data))
        print('Summary statistics on delays related to outgoing packets:')
        print('Node:      Name of node')
        print('Qid:       Identifier of transmit queue')
        print('Pkts:      Total number of packets transmitted by node or queue')
        print('NicP10:    10th percentile of NIC delay (time from xmit to NIC '
                'handoff)')
        print('NicP50:    Median NIC delay')
        print('NicP90:    90th percentile of NIC delay')
        print('GroP10:    10th percentile of GRO delay (maximum time across '
                'segments of TSO')
        print('           from NIC handoff to receipt by destination GRO)')
        print('GroP50:    Median GRO delay')
        print('GroP90:    90th percentile of GRO delay')
        print('FreP10:    10th percentile of free delay (time from NIC handoff '
                'to freeing')
        print('           transmit packet buffer)')
        print('FreP50:    Median free delay')
        print('FreP90:    90th percentile of free delay')

        first_node = True
        node_info = ''
        q_details = ''
        for node in get_sorted_nodes():
            if (options.node != None) and (node != options.node):
                continue

            # Create a data file for this node with packets in time order.
            pkts = sorted(node_pkts[node], key = lambda d : d['xmit'])
            if len(pkts) == 0:
                continue

            # Tx queue number -> dictionary mapping from delay type to a list
            # of delays of the given type on the given transmit queue.
            # Delay types currently used:
            # nic:        delay from xmit to nic doorbell
            # gro:        delay from xmit to gro on receiver
            # free:       delay from xmit to sk_buff free on sender
            delays = defaultdict(lambda: defaultdict(list))

            # Tx queue number -> total number of packets transmitted on that queue
            qid_counts = defaultdict(lambda: 0)

            total_pkts = 0

            f = open('%s/txpkts_%s.dat' % (options.data, node), 'w')
            f.write('# Node: %s\n' % (node))
            f.write('# Generated at %s.\n' %
                    (time.strftime('%I:%M %p on %m/%d/%Y')))
            f.write('# Data packets transmitted from %s:\n' % (node))
            f.write('# Xmit:       Time when packet was passed to ip*xmit\n')
            f.write('# RpcId:      Identifier of packet\'s RPC\n')
            f.write('# Offset:     Offset of packet within message\n')
            f.write('# Length:     Size of packet (before segmentation)\n')
            f.write('# Qid:        Transmit queue on which packet was sent\n')
            f.write('# Nic:        Time when packet was queued for NIC\n')
            f.write('# NDelay:     Nic - Xmit\n')
            f.write('# MaxGro:     Time when last fragment of packet was '
                    'received by GRO\n')
            f.write('# GDelay:     MaxGro - Nic\n')
            f.write('# Free:       Time when sk_buff was released on sender\n')
            f.write('# FDelay:     Free - Nic\n')
            f.write('# Rx:         Number of times segments in the packet were '
                    'retransmitted\n\n')

            f.write('#    Xmit      RpcId Offset  Length Qid')
            f.write('       Nic  NDelay    MaxGro  GDelay')
            f.write('      Free  FDelay Rx\n')
            for pkt in pkts:
                xmit = pkt['xmit']
                nic = pkt['nic']
                max_gro = get_max_gro(pkt)
                free = pkt['free_tx_skb']

                if 'tx_qid' in pkt:
                    qid = pkt['tx_qid']
                    qid_counts[qid] += 1
                    qid_string = str(qid)
                    if (options.tx_qid != None) and (qid != options.tx_qid):
                        continue
                else:
                    if options.tx_qid != None:
                        continue
                    qid = None
                    qid_string = ''
                total_pkts += 1

                rx = 0
                if 'retransmits' in pkt:
                    rx += len(pkt['retransmits'])
                if 'segments' in pkt:
                    for seg in pkt['segments']:
                        if 'retransmits' in seg:
                            rx += len(seg['retransmits'])
                rx_msg = str(rx) if rx > 0 else ""

                if rx == 0 and qid != None:
                    delays[qid]['nic'].append(nic - xmit)
                    delays[qid]['gro'].append(max_gro - nic)
                    delays[qid]['free'].append(free - nic)

                f.write('%9.3f %10d %6d  %6d %3s' % (xmit, pkt['id'],
                        pkt['offset'], pkt['tso_length'], qid_string))
                f.write(' %9.3f %7.1f %9.3f %7.1f' % (nic, nic - xmit,
                        max_gro, max_gro - nic))
                f.write(' %9.3f %7.1f %2s\n' % (pkt['free_tx_skb'],
                        pkt['free_tx_skb'] - nic, rx_msg))
            f.close()

            def print_type(delays):
                delays.sort()
                count = len(delays)
                if count > 0:
                    return '%6.1f %6.1f %6.1f' % (delays[10*count//100],
                            delays[50*count//100], delays[90*count//100])
                return ' '*20

            # Generate overall statistics by node and qid.
            if not first_node:
                q_details += '\n'
            q_details += 'Transmit queues for %s\n' % (node)
            q_details += 'Qid   Pkts NicP10 NicP50 NicP90   GroP10 GroP50 '
            q_details += 'GroP90   FreP10 FreP50 FreP90\n'
            q_details += '------------------------------------------------'
            q_details += '------------------------------\n'
            first_node = False
            totals = defaultdict(list)
            for qid in sorted(delays.keys()):
                q_delays = delays[qid]
                for type, d in q_delays.items():
                    totals[type].extend(d)
                q_details += '%4d %5d %s   %s   %s\n' % (
                        qid, qid_counts[qid],
                        print_type(q_delays['nic']),
                        print_type(q_delays['gro']),
                        print_type(q_delays['free']))
            node_info += '%-10s %5d %s   %s   %s\n' % (
                    node, total_pkts,
                    print_type(totals['nic']),
                    print_type(totals['gro']),
                    print_type(totals['free']))
        if not node_info:
            print('No packet data available')
        else:
            print('\nNode totals')
            print('Node        Pkts NicP10 NicP50 NicP90   GroP10 GroP50 GroP90'
                    '   FreP10 FreP50 FreP90')
            print('------------------------------------------------------------'
                    '-----------------------')
            print(node_info)
            print(q_details, end='')

#------------------------------------------------
# Analyzer: txqueues
#------------------------------------------------
class AnalyzeTxqueues:
    """
    Prints estimates of the amount of outbound packet data queued in the
    NIC of each node, assuming that the NIC transmits at full link speed.
    The --gbps option specifies the rate at which packets are transmitted.
    With --data option, generates detailed timelines of NIC queue lengths.
    """

    def __init__(self, dispatcher):
        # Node name -> list of <time, length, queue_length> tuples for all
        # transmitted packets. Length is the packet length including Homa
        # header but not IP or Ethernet overheads. Queue_length is the
        # # bytes in the NIC queue as of time (includes this packet).
        # Queue_length starts off zero and is updated later.
        self.nodes = defaultdict(list)

    def tt_send_data(self, trace, time, core, id, offset, length):
        self.nodes[trace['node']].append([time, length + 60, 0])

    def tt_send_grant(self, trace, time, core, id, offset, priority, increment):
        self.nodes[trace['node']].append([time, 34, 0])

    def output(self):
        global options, traces

        print('\n-------------------')
        print('Analyzer: txqueues')
        print('-------------------')

        # Compute queue lengths, find maximum for each node.
        print('Worst-case length of NIC tx queue for each node, assuming a link')
        print('speed of %.1f Gbps (change with --gbps):' % (options.gbps))
        print('Node:        Name of node')
        print('MaxLength:   Highest estimated output queue length for NIC (bytes)')
        print('Time:        Time when worst-case queue length occurred')
        print('Delay:       Delay (usec until fully transmitted) experienced by packet ')
        print('             transmitted at Time')
        print('')
        print('Node        MaxLength       Time   Delay')

        for node in get_sorted_nodes():
            pkts = self.nodes[node]
            if not pkts:
                continue
            pkts.sort()
            max_queue = 0
            max_time = 0
            cur_queue = 0
            prev_time = traces[node]['first_time']
            for i in range(len(pkts)):
                time, length, ignore = pkts[i]

                # 20 bytes for IPv4 header, 42 bytes for Ethernet overhead (CRC,
                # preamble, interpacket gap)
                total_length = length + 62

                xmit_bytes = ((time - prev_time) * (1000.0*options.gbps/8))
                if xmit_bytes < cur_queue:
                    cur_queue -= xmit_bytes
                else:
                    cur_queue = 0
                if 0 and (node == 'node6'):
                    if cur_queue == 0:
                        print('%9.3f (+%4.1f): length %6d, queue empty' %
                                (time, time - prev_time, total_length))
                    else:
                        print('%9.3f (+%4.1f): length %6d, xmit %5d, queue %6d -> %6d' %
                                (time, time - prev_time, total_length,
                                xmit_bytes, cur_queue, cur_queue + total_length))
                cur_queue += total_length
                if cur_queue > max_queue:
                    max_queue = cur_queue
                    max_time = time
                prev_time = time
                pkts[i][2] = cur_queue
            print('%-10s  %9d  %9.3f %7.1f ' % (node, max_queue, max_time,
                    (max_queue*8)/(options.gbps*1000)))

        if options.data:
            # Print stats for each node at regular intervals
            file = open('%s/txqueues.dat' % (options.data), 'w')
            line = 'Interval'
            for node in get_sorted_nodes():
                line += ' %10s' % (node)
            print(line, file=file)

            interval = options.interval
            interval_end = get_first_interval_end()
            end = get_last_time()

            # Node name -> current index in that node's packets
            cur = {}
            for node in get_sorted_nodes():
                cur[node] = 0

            while True:
                line = '%8.1f' % (interval_end)
                for node in get_sorted_nodes():
                    max = -1
                    i = cur[node]
                    xmits = self.nodes[node]
                    while i < len(xmits):
                        time, ignore, queue_length = xmits[i]
                        if time > interval_end:
                            break
                        if queue_length > max:
                            max = queue_length
                        i += 1
                    cur[node] = i
                    if max == -1:
                        line += ' ' * 11
                    else:
                        line += '   %8d' % (max)
                print(line, file=file)
                if interval_end > end:
                    break
                interval_end += interval
            file.close()

# Parse command-line options.
parser = OptionParser(description=
        'Read in one or more Homa timetrace files and use one or more '
        'analyzers to print information extracted from the file(s). The '
        'trace files should be synchronized (collected at about the same '
        'time and clock-synced with ttsync.py). Command-line arguments '
        'determine which analyzers to apply and provide additional '
        'parameters for the analyzers.',
        usage='%prog [options] [trace trace ...]',
        conflict_handler='resolve')
parser.add_option('--analyzers', '-a', dest='analyzers', default='all',
        metavar='A', help='Space-separated list of analyzers to apply to '
        'the trace files (default: all)')
parser.add_option('--core', dest='core', type=int, default=None,
        metavar='C', help='Specifies the number of a particular core of '
        'interest; required by some analyzers')
parser.add_option('--data', '-d', dest='data', default=None,
        metavar='DIR', help='If this option is specified, analyzers will '
        'output data files (suitable for graphing) in the directory given '
        'by DIR. If this option is not specified, no data files will '
        'be generated')
parser.add_option('--gbps', dest='gbps', type=float, default=100.0,
        metavar='G', help='Link speed in Gbps (default: 100); used by some '
        'analyzers.')
parser.add_option('-h', '--help', dest='help', action='store_true',
                  help='Show this help message and exit')
parser.add_option('--interval', dest='interval', type=int, default=20,
        metavar='T', help='Specifies the length of intervals for '
        'interval-based output, in microseconds (default: 20)')
parser.add_option('--late', dest='late', type=int, default=100,
        metavar='T', help='Specifies how long a packet must be delayed '
        'before it is considered overdue, in microseconds (default: 100)')
parser.add_option('--negative-ok', action='store_true', default=False,
        dest='negative_ok',
        help='Don\'t print warnings when negative delays are encountered')
parser.add_option('--node', dest='node', default=None,
        metavar='N', help='Specifies a particular node (the name of its '
        'trace file without the extension); required by some analyzers')
parser.add_option('--max-rtt', dest='max_rtt', type=float, default=None,
        metavar='T', help='Only consider RPCs with RTTs <= T usecs.  Used by '
        'rpc analyzer to select which specific RTTs to print out.')
parser.add_option('--pkt', dest='pkt', default=None,
        metavar='ID:OFF', help='Identifies a specific packet with ID:OFF, '
        'where ID is the RPC id on the sender (even means request message, '
        'odd means response) and OFF is an offset in the message; if this '
        'option is specified, some analyzers will output information specific '
        'to that packet.')
parser.add_option('--rx-core', dest='rx_core', type=int, default=None,
        metavar='C', help='If specified, some analyzers will ignore packets '
        'transmitted from cores other than C')
parser.add_option('--rx-end', dest='rx_end', type=float, default=None,
        metavar='T', help='If specified, some analyzers will ignore packets '
        'received at or after time T')
parser.add_option('--rx-node', dest='rx_node', default=None,
        metavar='N', help='If specified, some analyzers will ignore packets '
        'received by nodes other than N')
parser.add_option('--rx-start', dest='rx_start', type=float, default=None,
        metavar='T', help='If specified, some analyzers will ignore packets '
        'received before time T')
parser.add_option('--time', dest='time', type=float, default=None,
        metavar='T', help='Time of interest; required by some analyzers')
parser.add_option('--tx-core', dest='tx_core', type=int, default=None,
        metavar='C', help='If specified, some analyzers will ignore packets '
        'transmitted from cores other than C')
parser.add_option('--tx-end', dest='tx_end', type=float, default=None,
        metavar='T', help='If specified, some analyzers will ignore packets '
        'transmitted at or after time T')
parser.add_option('--tx-qid', dest='tx_qid', type=int, default=None,
        metavar='C', help='Specifies a transmit queue identifier; used '
        'by some anlyzers to select a specific queue.')
parser.add_option('--tx-node', dest='tx_node', default=None,
        metavar='N', help='If specified, some analyzers will ignore ignore packets '
        'transmitted by nodes other than N')
parser.add_option('--tx-start', dest='tx_start', type=float, default=None,
        metavar='T', help='If specified, some analyzers will ignore packets '
        'transmitted before time T')
parser.add_option('--verbose', '-v', action='store_true', default=False,
        dest='verbose',
        help='Print additional output with more details')

(options, tt_files) = parser.parse_args()
if options.help:
    parser.print_help()
    print("\nAvailable analyzers:")
    print_analyzer_help()
    exit(0)
if not tt_files:
    print('No trace files specified')
    exit(1)
if options.data:
    os.makedirs(options.data, exist_ok=True)
if options.pkt:
    match = re.match('([0-9]+):([0-9]+)$', options.pkt)
    if not match:
        print('Bad value "%s" for --pkt option; must be id:offset'
                % (options.pkt), file=sys.stderr)
        exit(1)
    options.pkt_id = int(match.group(1))
    options.pkt_offset = int(match.group(2))
d = Dispatcher()
analyzer_classes = []
for name in options.analyzers.split():
    class_name = 'Analyze' + name[0].capitalize() + name[1:]
    if not hasattr(sys.modules[__name__], class_name):
        print('No analyzer named "%s"' % (name), file=sys.stderr)
        exit(1)
    d.interest(class_name)
    analyzer_classes.append(class_name)

# Parse the timetrace files; this will invoke handlers in the analyzers.
for file in tt_files:
    d.parse(file)

d.print_no_matches()

if options.verbose:
    d.print_stats()

# Invoke 'analyze' methods in each analyzer, if present, to perform
# postprocessing now that all the trace data has been read.
for analyzer in d.get_analyzers():
    if hasattr(analyzer, 'analyze'):
        # print('Calling %s.analyze' % (type(analyzer).__name__), file=sys.stderr)
        analyzer.analyze()

# Give each analyzer a chance to output its findings (includes
# printing output and generating data files).
for name in analyzer_classes:
    analyzer = d.get_analyzer(name)
    if hasattr(analyzer, 'output'):
        analyzer.output()