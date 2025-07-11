#!/usr/bin/env python3

# This tool reads a disk image in any format and converts it to qcow2,
# writing the result directly to stdout.
#
# Copyright (C) 2024 Igalia, S.L.
#
# Authors: Alberto Garcia <berto@igalia.com>
#          Madeeha Javed <javed@igalia.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# qcow2 files produced by this script are always arranged like this:
#
# - qcow2 header
# - refcount table
# - refcount blocks
# - L1 table
# - L2 tables
# - Data clusters
#
# A note about variable names: in qcow2 there is one refcount table
# and one (active) L1 table, although each can occupy several
# clusters. For the sake of simplicity the code sometimes talks about
# refcount tables and L1 tables when referring to those clusters.

import argparse
import math
import os
import struct
import sys

QCOW2_DEFAULT_CLUSTER_SIZE = 65536
QCOW2_DEFAULT_REFCOUNT_BITS = 16
QCOW2_FEATURE_NAME_TABLE = 0x6803F857
QCOW2_DATA_FILE_NAME_STRING = 0x44415441
QCOW2_V3_HEADER_LENGTH = 112  # Header length in QEMU 9.0. Must be a multiple of 8
QCOW2_INCOMPAT_DATA_FILE_BIT = 2
QCOW2_AUTOCLEAR_DATA_FILE_RAW_BIT = 1
QCOW_OFLAG_COPIED = 1 << 63


def bitmap_set(bitmap, idx):
    bitmap[idx // 8] |= 1 << (idx % 8)


def bitmap_is_set(bitmap, idx):
    return (bitmap[idx // 8] & (1 << (idx % 8))) != 0


def bitmap_iterator(bitmap, length):
    for idx in range(length):
        if bitmap_is_set(bitmap, idx):
            yield idx


def align_up(num, d):
    return d * math.ceil(num / d)


def write_features(cluster, offset, data_file_name):
    if data_file_name is not None:
        encoded_name = data_file_name.encode("utf-8")
        padded_name_len = align_up(len(encoded_name), 8)
        struct.pack_into(f">II{padded_name_len}s", cluster, offset,
                         QCOW2_DATA_FILE_NAME_STRING,
                         len(encoded_name),
                         encoded_name)
        offset += 8 + padded_name_len

    qcow2_features = [
        # Incompatible
        (0, 0, "dirty bit"),
        (0, 1, "corrupt bit"),
        (0, 2, "external data file"),
        (0, 3, "compression type"),
        (0, 4, "extended L2 entries"),
        # Compatible
        (1, 0, "lazy refcounts"),
        # Autoclear
        (2, 0, "bitmaps"),
        (2, 1, "raw external data"),
    ]
    struct.pack_into(">I", cluster, offset, QCOW2_FEATURE_NAME_TABLE)
    struct.pack_into(">I", cluster, offset + 4, len(qcow2_features) * 48)
    offset += 8
    for feature_type, feature_bit, feature_name in qcow2_features:
        struct.pack_into(">BB46s", cluster, offset,
                         feature_type, feature_bit, feature_name.encode("ascii"))
        offset += 48


def write_qcow2_content(input_file, cluster_size, refcount_bits,
                        data_file_name, data_file_raw, diff_file_name):
    # Some basic values
    l1_entries_per_table = cluster_size // 8
    l2_entries_per_table = cluster_size // 8
    refcounts_per_table  = cluster_size // 8
    refcounts_per_block  = cluster_size * 8 // refcount_bits

    # Open the input file for reading
    fd = os.open(input_file, os.O_RDONLY)

    # Virtual disk size, number of data clusters and L1 entries
    block_device_size = os.lseek(fd, 0, os.SEEK_END)
    disk_size = align_up(block_device_size, 512)
    total_data_clusters = math.ceil(disk_size / cluster_size)
    l1_entries = math.ceil(total_data_clusters / l2_entries_per_table)
    allocated_l1_tables = math.ceil(l1_entries / l1_entries_per_table)

    # Max L1 table size is 32 MB (QCOW_MAX_L1_SIZE in block/qcow2.h)
    if (l1_entries * 8) > (32 * 1024 * 1024):
        sys.exit("[Error] The image size is too large. Try using a larger cluster size.")

    # Two bitmaps indicating which L1 and L2 entries are set
    l1_bitmap = bytearray(allocated_l1_tables * l1_entries_per_table // 8)
    l2_bitmap = bytearray(l1_entries * l2_entries_per_table // 8)
    allocated_l2_tables = 0
    allocated_data_clusters = 0

    if data_file_raw:
        # If data_file_raw is set then all clusters are allocated and
        # we don't need to read the input file at all.
        allocated_l2_tables = l1_entries
        for idx in range(l1_entries):
            bitmap_set(l1_bitmap, idx)
        for idx in range(total_data_clusters):
            bitmap_set(l2_bitmap, idx)
    else:
        # Allocates a cluster in the appropriate bitmaps if it's different
        # from cluster_to_compare_with
        def check_cluster_allocate(idx, cluster, cluster_to_compare_with):
            nonlocal allocated_data_clusters
            nonlocal allocated_l2_tables
            # If the last cluster is smaller than cluster_size pad it with zeroes
            if len(cluster) < cluster_size:
                cluster += bytes(cluster_size - len(cluster))
            # If a cluster has different data from the cluster_to_compare_with then it
            # must be allocated in the output file and its L2 entry must be set
            if cluster != cluster_to_compare_with:
                bitmap_set(l2_bitmap, idx)
                allocated_data_clusters += 1
                # Allocated data clusters also need their corresponding L1 entry and L2 table
                l1_idx = math.floor(idx / l2_entries_per_table)
                if not bitmap_is_set(l1_bitmap, l1_idx):
                    bitmap_set(l1_bitmap, l1_idx)
                    allocated_l2_tables += 1

        zero_cluster = bytes(cluster_size)
        last_cluster = align_up(block_device_size, cluster_size) // cluster_size
        if diff_file_name:
            # Read all the clusters that differ from the diff_file_name
            diff_fd = os.open(diff_file_name, os.O_RDONLY)
            diff_block_device_size = os.lseek(diff_fd, 0, os.SEEK_END)
            last_diff_cluster = align_up(diff_block_device_size, cluster_size) // cluster_size
            # In case input_file is bigger than diff_file_name, first check
            # if clusters from diff_file_name differ, and then check if the
            # rest contain data
            for idx in range(0, last_diff_cluster):
                cluster = os.pread(fd, cluster_size, cluster_size * idx)
                original_cluster = os.pread(diff_fd, cluster_size, cluster_size * idx)

                # If a cluster has different data from the original_cluster
                # then it must be allocated
                check_cluster_allocate(idx, cluster, original_cluster)
            for idx in range(last_diff_cluster, last_cluster):
                cluster = os.pread(fd, cluster_size, cluster_size * idx)

                # If a cluster has different data from the original_cluster
                # then it must be allocated
                check_cluster_allocate(idx, cluster, zero_cluster)
        else:
            # Read all the clusters that contain data
            for idx in range(0, last_cluster):
                cluster = os.pread(fd, cluster_size, cluster_size * idx)
                # If a cluster has non-zero data then it must be allocated
                check_cluster_allocate(idx, cluster, zero_cluster)

    # Total amount of allocated clusters excluding the refcount blocks and table
    total_allocated_clusters = 1 + allocated_l1_tables + allocated_l2_tables
    if data_file_name is None:
        total_allocated_clusters += allocated_data_clusters

    # Clusters allocated for the refcount blocks and table
    allocated_refcount_blocks = math.ceil(total_allocated_clusters  / refcounts_per_block)
    allocated_refcount_tables = math.ceil(allocated_refcount_blocks / refcounts_per_table)

    # Now we have a problem because allocated_refcount_blocks and allocated_refcount_tables...
    # (a) increase total_allocated_clusters, and
    # (b) need to be recalculated when total_allocated_clusters is increased
    # So we need to repeat the calculation as long as the numbers change
    while True:
        new_total_allocated_clusters = total_allocated_clusters + allocated_refcount_tables + allocated_refcount_blocks
        new_allocated_refcount_blocks = math.ceil(new_total_allocated_clusters / refcounts_per_block)
        if new_allocated_refcount_blocks > allocated_refcount_blocks:
            allocated_refcount_blocks = new_allocated_refcount_blocks
            allocated_refcount_tables = math.ceil(allocated_refcount_blocks / refcounts_per_table)
        else:
            break

    # Now that we have the final numbers we can update total_allocated_clusters
    total_allocated_clusters += allocated_refcount_tables + allocated_refcount_blocks

    # At this point we have the exact number of clusters that the output
    # image is going to use so we can calculate all the offsets.
    current_cluster_idx = 1

    refcount_table_offset = current_cluster_idx * cluster_size
    current_cluster_idx += allocated_refcount_tables

    refcount_block_offset = current_cluster_idx * cluster_size
    current_cluster_idx += allocated_refcount_blocks

    l1_table_offset = current_cluster_idx * cluster_size
    current_cluster_idx += allocated_l1_tables

    l2_table_offset = current_cluster_idx * cluster_size
    current_cluster_idx += allocated_l2_tables

    data_clusters_offset = current_cluster_idx * cluster_size

    # Calculate some values used in the qcow2 header
    if allocated_l1_tables == 0:
        l1_table_offset = 0

    hdr_cluster_bits = int(math.log2(cluster_size))
    hdr_refcount_bits = int(math.log2(refcount_bits))
    hdr_length = QCOW2_V3_HEADER_LENGTH
    hdr_incompat_features = 0
    if data_file_name is not None:
        hdr_incompat_features |= 1 << QCOW2_INCOMPAT_DATA_FILE_BIT
    hdr_autoclear_features = 0
    if data_file_raw:
        hdr_autoclear_features |= 1 << QCOW2_AUTOCLEAR_DATA_FILE_RAW_BIT

    ### Write qcow2 header
    cluster = bytearray(cluster_size)
    struct.pack_into(">4sIQIIQIIQQIIQQQQII", cluster, 0,
        b"QFI\xfb",            # QCOW magic string
        3,                     # version
        0,                     # backing file offset
        0,                     # backing file sizes
        hdr_cluster_bits,
        disk_size,
        0,                     # encryption method
        l1_entries,
        l1_table_offset,
        refcount_table_offset,
        allocated_refcount_tables,
        0,                     # number of snapshots
        0,                     # snapshot table offset
        hdr_incompat_features,
        0,                     # compatible features
        hdr_autoclear_features,
        hdr_refcount_bits,
        hdr_length,
    )

    write_features(cluster, hdr_length, data_file_name)

    sys.stdout.buffer.write(cluster)

    ### Write refcount table
    cur_offset = refcount_block_offset
    remaining_refcount_table_entries = allocated_refcount_blocks # Each entry is a pointer to a refcount block
    while remaining_refcount_table_entries > 0:
        cluster = bytearray(cluster_size)
        to_write = min(remaining_refcount_table_entries, refcounts_per_table)
        remaining_refcount_table_entries -= to_write
        for idx in range(to_write):
            struct.pack_into(">Q", cluster, idx * 8, cur_offset)
            cur_offset += cluster_size
        sys.stdout.buffer.write(cluster)

    ### Write refcount blocks
    remaining_refcount_block_entries = total_allocated_clusters # One entry for each allocated cluster
    for tbl in range(allocated_refcount_blocks):
        cluster = bytearray(cluster_size)
        to_write = min(remaining_refcount_block_entries, refcounts_per_block)
        remaining_refcount_block_entries -= to_write
        # All refcount entries contain the number 1. The only difference
        # is their bit width, defined when the image is created.
        for idx in range(to_write):
            if refcount_bits == 64:
                struct.pack_into(">Q", cluster, idx * 8, 1)
            elif refcount_bits == 32:
                struct.pack_into(">L", cluster, idx * 4, 1)
            elif refcount_bits == 16:
                struct.pack_into(">H", cluster, idx * 2, 1)
            elif refcount_bits == 8:
                cluster[idx] = 1
            elif refcount_bits == 4:
                cluster[idx // 2] |= 1 << ((idx % 2) * 4)
            elif refcount_bits == 2:
                cluster[idx // 4] |= 1 << ((idx % 4) * 2)
            elif refcount_bits == 1:
                cluster[idx // 8] |= 1 << (idx % 8)
        sys.stdout.buffer.write(cluster)

    ### Write L1 table
    cur_offset = l2_table_offset
    for tbl in range(allocated_l1_tables):
        cluster = bytearray(cluster_size)
        for idx in range(l1_entries_per_table):
            l1_idx = tbl * l1_entries_per_table + idx
            if bitmap_is_set(l1_bitmap, l1_idx):
                struct.pack_into(">Q", cluster, idx * 8, cur_offset | QCOW_OFLAG_COPIED)
                cur_offset += cluster_size
        sys.stdout.buffer.write(cluster)

    ### Write L2 tables
    cur_offset = data_clusters_offset
    for tbl in range(l1_entries):
        # Skip the empty L2 tables. We can identify them because
        # there is no L1 entry pointing at them.
        if bitmap_is_set(l1_bitmap, tbl):
            cluster = bytearray(cluster_size)
            for idx in range(l2_entries_per_table):
                l2_idx = tbl * l2_entries_per_table + idx
                if bitmap_is_set(l2_bitmap, l2_idx):
                    if data_file_name is None:
                        struct.pack_into(">Q", cluster, idx * 8, cur_offset | QCOW_OFLAG_COPIED)
                        cur_offset += cluster_size
                    else:
                        struct.pack_into(">Q", cluster, idx * 8, (l2_idx * cluster_size) | QCOW_OFLAG_COPIED)
            sys.stdout.buffer.write(cluster)

    ### Write data clusters
    if data_file_name is None:
        for idx in bitmap_iterator(l2_bitmap, total_data_clusters):
            cluster = os.pread(fd, cluster_size, cluster_size * idx)
            # If the last cluster is smaller than cluster_size pad it with zeroes
            if len(cluster) < cluster_size:
                cluster += bytes(cluster_size - len(cluster))
            sys.stdout.buffer.write(cluster)

    if not data_file_raw:
        os.close(fd)


def main():
    # Command-line arguments
    parser = argparse.ArgumentParser(
        description="This program converts a QEMU disk image to qcow2 "
        "and writes it to the standard output"
    )
    parser.add_argument("input_file", help="name of the input file")
    parser.add_argument(
        "--diff",
        dest="diff_file_name",
        metavar="diff_file_name",
        help=("name of the original file to compare input_file against. "
                "If specified, will only export clusters that are different "
                "between the files"),
        default=None,
    )
    parser.add_argument(
        "-c",
        dest="cluster_size",
        metavar="cluster_size",
        help=f"qcow2 cluster size (default: {QCOW2_DEFAULT_CLUSTER_SIZE})",
        default=QCOW2_DEFAULT_CLUSTER_SIZE,
        type=int,
        choices=[1 << x for x in range(9, 22)],
    )
    parser.add_argument(
        "-r",
        dest="refcount_bits",
        metavar="refcount_bits",
        help=f"width of the reference count entries (default: {QCOW2_DEFAULT_REFCOUNT_BITS})",
        default=QCOW2_DEFAULT_REFCOUNT_BITS,
        type=int,
        choices=[1 << x for x in range(7)],
    )
    parser.add_argument(
        "-d",
        dest="data_file",
        help="create an image with input_file as an external data file",
        action="store_true",
    )
    parser.add_argument(
        "-R",
        dest="data_file_raw",
        help="enable data_file_raw on the generated image (implies -d)",
        action="store_true",
    )
    args = parser.parse_args()

    if args.data_file_raw:
        args.data_file = True

    if not os.path.exists(args.input_file):
        sys.exit(f"[Error] {args.input_file} does not exist.")

    if args.diff_file_name and not os.path.exists(args.diff_file_name):
        sys.exit(f"[Error] {args.diff_file_name} does not exist.")

    # A 512 byte header is too small for the data file name extension
    if args.data_file and args.cluster_size == 512:
        sys.exit("[Error] External data files require a larger cluster size")

    if sys.stdout.isatty():
        sys.exit("[Error] Refusing to write to a tty. Try redirecting stdout.")

    if args.data_file:
        data_file_name = args.input_file
    else:
        data_file_name = None

    write_qcow2_content(
        args.input_file,
        args.cluster_size,
        args.refcount_bits,
        data_file_name,
        args.data_file_raw,
        args.diff_file_name
    )


if __name__ == "__main__":
    main()

