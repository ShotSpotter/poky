# ex:ts=4:sw=4:sts=4:et
# -*- tab-width: 4; c-basic-offset: 4; indent-tabs-mode: nil -*-
#
# Copyright (c) 2013, Intel Corporation.
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# DESCRIPTION

# This module implements the image creation engine used by 'wic' to
# create images.  The engine parses through the OpenEmbedded kickstart
# (wks) file specified and generates images that can then be directly
# written onto media.
#
# AUTHORS
# Tom Zanussi <tom.zanussi (at] linux.intel.com>
#

import os
import sys
from abc import ABCMeta, abstractmethod
import shlex
import json
import subprocess
import shutil

import os, sys, errno
from mic import msger, creator
from mic.utils import cmdln, misc, errors
from mic.conf import configmgr
from mic.plugin import pluginmgr
from mic.__version__ import VERSION
from mic.utils.oe.misc import *


def verify_build_env():
    """
    Verify that the build environment is sane.

    Returns True if it is, false otherwise
    """
    try:
        builddir = os.environ["BUILDDIR"]
    except KeyError:
        print "BUILDDIR not found, exiting. (Did you forget to source oe-init-build-env?)"
        sys.exit(1)

    return True


def find_artifacts(image_name):
    """
    Gather the build artifacts for the current image (the image_name
    e.g. core-image-minimal) for the current MACHINE set in local.conf
    """
    bitbake_env_lines = get_bitbake_env_lines()

    rootfs_dir = kernel_dir = hdddir = staging_data_dir = native_sysroot = ""

    for line in bitbake_env_lines.split('\n'):
        if (get_line_val(line, "IMAGE_ROOTFS")):
            rootfs_dir = get_line_val(line, "IMAGE_ROOTFS")
            continue
        if (get_line_val(line, "STAGING_KERNEL_DIR")):
            kernel_dir = get_line_val(line, "STAGING_KERNEL_DIR")
            continue
        if (get_line_val(line, "HDDDIR")):
            hdddir = get_line_val(line, "HDDDIR")
            continue
        if (get_line_val(line, "STAGING_DATADIR")):
            staging_data_dir = get_line_val(line, "STAGING_DATADIR")
            continue
        if (get_line_val(line, "STAGING_DIR_NATIVE")):
            native_sysroot = get_line_val(line, "STAGING_DIR_NATIVE")
            continue

    return (rootfs_dir, kernel_dir, hdddir, staging_data_dir, native_sysroot)


CANNED_IMAGE_DIR = "lib/image/canned-wks" # relative to scripts

def find_canned_image(scripts_path, wks_file):
    """
    Find a .wks file with the given name in the canned files dir.

    Return False if not found
    """
    canned_wks_dir = os.path.join(scripts_path, CANNED_IMAGE_DIR)

    for root, dirs, files in os.walk(canned_wks_dir):
        for file in files:
            if file.endswith("~") or file.endswith("#"):
                continue
            if file.endswith(".wks") and wks_file + ".wks" == file:
                fullpath = os.path.join(canned_wks_dir, file)
                return fullpath
    return None


def list_canned_images(scripts_path):
    """
    List the .wks files in the canned image dir, minus the extension.
    """
    canned_wks_dir = os.path.join(scripts_path, CANNED_IMAGE_DIR)

    for root, dirs, files in os.walk(canned_wks_dir):
        for file in files:
            if file.endswith("~") or file.endswith("#"):
                continue
            if file.endswith(".wks"):
                fullpath = os.path.join(canned_wks_dir, file)
                f = open(fullpath, "r")
                lines = f.readlines()
                for line in lines:
                    desc = ""
                    idx = line.find("short-description:")
                    if idx != -1:
                        desc = line[idx + len("short-description:"):].strip()
                        break
                basename = os.path.splitext(file)[0]
                print "  %s\t\t%s" % (basename, desc)


def list_canned_image_help(scripts_path, fullpath):
    """
    List the help and params in the specified canned image.
    """
    canned_wks_dir = os.path.join(scripts_path, CANNED_IMAGE_DIR)

    f = open(fullpath, "r")
    lines = f.readlines()
    found = False
    for line in lines:
        if not found:
            idx = line.find("long-description:")
            if idx != -1:
                print
                print line[idx + len("long-description:"):].strip()
                found = True
            continue
        if not line.strip():
            break
        idx = line.find("#")
        if idx != -1:
            print line[idx + len("#:"):].rstrip()
        else:
            break


def wic_create(args, wks_file, rootfs_dir, bootimg_dir, kernel_dir,
               native_sysroot, hdddir, staging_data_dir, scripts_path,
               image_output_dir, debug, properties_file, properties=None):
    """
    Create image

    wks_file - user-defined OE kickstart file
    rootfs_dir - absolute path to the build's /rootfs dir
    bootimg_dir - absolute path to the build's boot artifacts directory
    kernel_dir - absolute path to the build's kernel directory
    native_sysroot - absolute path to the build's native sysroots dir
    hdddir - absolute path to the build's HDDDIR dir
    staging_data_dir - absolute path to the build's STAGING_DATA_DIR dir
    scripts_path - absolute path to /scripts dir
    image_output_dir - dirname to create for image
    properties_file - use values from this file if nonempty i.e no prompting
    properties - use values from this string if nonempty i.e no prompting

    Normally, the values for the build artifacts values are determined
    by 'wic -e' from the output of the 'bitbake -e' command given an
    image name e.g. 'core-image-minimal' and a given machine set in
    local.conf.  If that's the case, the variables get the following
    values from the output of 'bitbake -e':

    rootfs_dir:        IMAGE_ROOTFS
    kernel_dir:        STAGING_KERNEL_DIR
    native_sysroot:    STAGING_DIR_NATIVE
    hdddir:            HDDDIR
    staging_data_dir:  STAGING_DATA_DIR

    In the above case, bootimg_dir remains unset and the image
    creation code determines which of the passed-in directories to
    use.

    In the case where the values are passed in explicitly i.e 'wic -e'
    is not used but rather the individual 'wic' options are used to
    explicitly specify these values, hdddir and staging_data_dir will
    be unset, but bootimg_dir must be explicit i.e. explicitly set to
    either hdddir or staging_data_dir, depending on the image being
    generated.  The other values (rootfs_dir, kernel_dir, and
    native_sysroot) correspond to the same values found above via
    'bitbake -e').

    """
    try:
        oe_builddir = os.environ["BUILDDIR"]
    except KeyError:
        print "BUILDDIR not found, exiting. (Did you forget to source oe-init-build-env?)"
        sys.exit(1)

    direct_args = list()
    direct_args.insert(0, oe_builddir)
    direct_args.insert(0, image_output_dir)
    direct_args.insert(0, wks_file)
    direct_args.insert(0, rootfs_dir)
    direct_args.insert(0, bootimg_dir)
    direct_args.insert(0, kernel_dir)
    direct_args.insert(0, native_sysroot)
    direct_args.insert(0, hdddir)
    direct_args.insert(0, staging_data_dir)
    direct_args.insert(0, "direct")

    if debug:
        msger.set_loglevel('debug')

    cr = creator.Creator()

    cr.main(direct_args)

    print "\nThe image(s) were created using OE kickstart file:\n  %s" % wks_file


def wic_list(args, scripts_path, properties_file):
    """
    Print the complete list of properties defined by the image, or the
    possible values for a particular image property.
    """
    if len(args) < 1:
        return False

    if len(args) == 1:
        if args[0] == "images":
            list_canned_images(scripts_path)
            return True
        elif args[0] == "properties":
            return True
        else:
            return False

    if len(args) == 2:
        if args[0] == "properties":
            wks_file = args[1]
            print "print properties contained in wks file: %s" % wks_file
            return True
        elif args[0] == "property":
            print "print property values for property: %s" % args[1]
            return True
        elif args[1] == "help":
            wks_file = args[0]
            fullpath = find_canned_image(scripts_path, wks_file)
            if not fullpath:
                print "No image named %s found, exiting.  (Use 'wic list images' to list available images, or specify a fully-qualified OE kickstart (.wks) filename)\n" % wks_file
                sys.exit(1)
            list_canned_image_help(scripts_path, fullpath)
            return True
        else:
            return False

    return False
