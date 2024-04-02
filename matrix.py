import logging
import multiline
import re
import requests
from collections import defaultdict
from functools import cmp_to_key
from py_markdown_table.markdown_table import markdown_table

hwsupport_url_prefix = "https://www.cisco.com/c/dam/en/us/td/docs/Website/datacenter/acihwsupport/db/db_hwSupport"  # noqa: E501
apicrelease_url = "https://www.cisco.com/c/dam/en/us/td/docs/Website/datacenter/_common/js/db_apicReleases.js"  # noqa: E501

MIN_VER = "4.2(1)"
PID_KEY = "Product Model"  # table header for per PID tables
VERSION_KEY = "Version"  # table header for chronological table

PID_TYPES = [
    # APIC
    ["APIC Servers"],
    # Fixed Leaf Switches
    ["Fixed Leaf Switches", "Top-of-rack (ToR) leaf switch", "Leaf switch"],
    # Fixed Spine Switches
    ["Fixed Spine Switches", "Spine switch"],
    # Modular Leaf Line Cards
    ["Modular Leaf Switch Line Cards"],
    # Modular Leaf SUP
    [
        "Modular Leaf Switch Supervisor",
        "Modular Leaf Switch Supervisor and System Controller Modules",
    ],
    # Modular Leaf Switches
    ["Modular Leaf Switches"],
    # Modular Spine Fabric Cards
    ["Modular Spine Switch Fabric Modules", "Spine switch module"],
    # Modular Spine Line Cards
    ["Modular Spine Switch Line Cards", "Spine switch module"],
    # Modular Spine SUP and SC
    ["Modular Spine Switch Supervisor and System Controller Modules", "Switch module"],
    # Modular Spine Chassis
    ["Modular Spine Switches", "Spine switch", "Chassis"],
]
PID_TYPES_TO_IGNORE = [
    "Pluggable module (GEM)",
    "Expansion Modules",
    "Modular Spine Switch Fans",
    "Spine switch fan",
    "Chassis component",
    "Fixed Spine Switch Power Supply Units",
    "Fixed Spine Switch Fans",
    "Fixed Spine Switch Fan",
    "Fixed Leaf Switch Power Supply Units",
    "Fixed Leaf Switch Power Supply Unit",
    "Leaf switch power supply unit",
    "Fixed Leaf Switch Fans",
    "Fixed Leaf Switch Fan",
    "Leaf switch fan",
    "Top-of-rack (ToR) leaf switch power supply unit",
    "Top-of-rack (ToR) leaf switch fan",
]
# db_hwSupportXXXX.js for old versions use the same type for different switches
# even within the same version. Hardcoding only those exceptions.
DIRTY_PIDS = {
    # db_hwSupportXXXX.js may say "Spine switch"
    "Modular Spine Switches": [
        "N9K-C9508-B1",
        "N9K-C9508-B2",
        "N9K-C9516",
    ],
    # db_hwSupportXXXX.js may say "Spine switch"
    "Fixed Spine Switches": [
        "N9K-C9336PQ",
        "N9K-C9332C",
        "N9K-C9364C",
    ],
    # db_hwSupportXXXX.js may say "Spine switch module"
    "Modular Spine Switch Line Cards": [
        "N9K-X9732C-EX",
        "N9K-X9736PQ",
        "N9K-X9736C-FX",
        "N9K-X9736Q-FX",  # listed as Spine switch in 1411,1412.
    ],
    # db_hwSupportXXXX.js may say "Spine switch module"
    "Modular Spine Switch Fabric Modules": [
        "N9K-C9504-FM",
        "N9K-C9504-FM-E",
        "N9K-C9508-FM",
        "N9K-C9508-FM-E",
        "N9K-C9508-FM-E2",
        "N9K-C9516-FM",
        "N9K-C9516-FM-E2",
    ],
}
PIDS_TO_IGNORE = [
    "Cisco Nexus 9504",  # this is a duplicate and wrong entry listed as a line card
    "",  # this empty PID is listed in 1603
]
# db_hwSupportXXXX.js for old versions do not list APICs even when they are supported.
# Hardcoding minimum supported version for those. 1.x versions are out of scope.
# These should be used only for db_hwSupportXXXX.js for versions older than 4.2(1)
# as db_hwSupportXXXX.js lists APICs from 4.2(1) and APICs that are not listed are
# not supported on those new versions.
MIN_SUPPORT_VERSIONS = {
    "APIC-M1": {"min": "2.0(1)", "valid": "4.2(1)"},
    "APIC-L1": {"min": "2.0(1)", "valid": "4.2(1)"},
    "APIC-M2": {"min": "2.0(1)", "valid": "4.2(1)"},
    "APIC-L2": {"min": "2.0(1)", "valid": "4.2(1)"},
    "APIC-M3": {"min": "4.0(1)", "valid": "4.2(1)"},
    "APIC-L3": {"min": "4.0(1)", "valid": "4.2(1)"},
}


MD_ICON_SUPPORTED = ":white_check_mark:"
MD_ICON_NOTSUPPORTED = ":no_entry_sign:"

logging.basicConfig(
    format="[%(asctime)s %(levelname)s %(name)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
    level=logging.DEBUG,
)


class AciVersion:
    v_regex = r"(?:dk9\.)?[1]?(?P<major1>\d)\.(?P<major2>\d)(?:\.|\()(?P<maint>\d+)\.?(?P<patch>(?:[a-b]|[0-9a-z]+))?\)?"  # noqa: E501

    def __init__(self, version):
        self.original = version
        v = re.search(self.v_regex, version)
        self.version = (
            "{major1}.{major2}({maint}{patch})".format(**v.groupdict()) if v else None
        )
        self.dot_version = (
            "{major1}.{major2}.{maint}{patch}".format(**v.groupdict()) if v else None
        )
        self.simple_version = (
            "{major1}.{major2}({maint})".format(**v.groupdict()) if v else None
        )
        self.compressed_version = (
            "{major1}{major2}{maint}".format(**v.groupdict()) if v else None
        )
        self.major1 = v.group("major1") if v else None
        self.major2 = v.group("major2") if v else None
        self.maint = v.group("maint") if v else None
        self.patch = v.group("patch") if v else None
        self.regex = v
        if not v:
            raise RuntimeError(f"Parsing failure of ACI version `{version}`")

    def __str__(self):
        return self.version

    def older_than(self, version):
        v = re.search(self.v_regex, version)
        if not v:
            return None
        for i in range(1, len(v.groups()) + 1):
            if i < 4:
                if int(self.regex.group(i)) > int(v.group(i)):
                    return False
                elif int(self.regex.group(i)) < int(v.group(i)):
                    return True
            if i == 4 and v.group(i) and self.patch:
                if self.regex.group(i) > v.group(i):
                    return False
                elif self.regex.group(i) < v.group(i):
                    return True
        return False

    def newer_than(self, version):
        return not self.older_than(version) and not self.same_as(version)

    def same_as(self, version):
        v = re.search(self.v_regex, version)
        ver = "{major1}.{major2}({maint}{patch})".format(**v.groupdict()) if v else None
        return self.version == ver


def version_sort(version1, version2):
    v1 = AciVersion(version1)
    if v1.newer_than(version2):
        return 1
    elif v1.older_than(version2):
        return -1
    else:
        return 0


def get_taffy_db(url: str):
    js = requests.get(url)

    taffy_regex = re.compile(r"TAFFY\((.*)\)", re.DOTALL)
    r = taffy_regex.search(js.text)
    json_str = r.group(1)
    contents = multiline.loads(json_str, multiline=True)

    return contents


def get_pid_type(pid_type: str, pid: str):
    """Make sure to use the same PID type across versions.
    Each db_hwSupportXXXX.js uses a different type name for the same PID.
    """
    # PID in DIRTY_PIDS is known to have a wrong PID type/category. Hardcode them.
    for _pid_type, pids in DIRTY_PIDS.items():
        if pid in pids:
            return _pid_type
    # Inconsistent PID type but they are still in the correct category.
    # Always pick the same type name from the same category.
    for type_list in PID_TYPES:
        if pid_type in type_list:
            return type_list[0]
    logging.error(f"Unknown PID Type - {pid_type}")
    return pid_type


def create_per_pid_data(ptype_ver_pid: dict, all_versions: list):
    per_pid = {
        "full": defaultdict(list),  # all versions
        "newer": defaultdict(list),  # only versions newer than MIN_VER
    }
    for pid_type, p2v in ptype_ver_pid.items():
        for pid, supported_versions in p2v.items():
            full_versions = {}
            newer_versions = {}
            # Convert supported/not supported into emoji
            for version in all_versions:
                v = AciVersion(version)
                if v.simple_version in supported_versions:
                    icon = MD_ICON_SUPPORTED
                else:
                    icon = MD_ICON_NOTSUPPORTED

                full_versions[version] = icon
                if not v.older_than(MIN_VER):
                    newer_versions[version] = icon

            # Create table data for each PID type separately
            per_pid["full"][pid_type].append(
                {
                    **{PID_KEY: pid},
                    **full_versions,
                }
            )
            per_pid["newer"][pid_type].append(
                {
                    **{PID_KEY: pid},
                    **newer_versions,
                }
            )
    return per_pid


def create_chronological_data(ver_ptype_pid: dict):
    per_version = []
    prev_version = {pid_type[0]: [] for pid_type in PID_TYPES}
    for version in ver_ptype_pid:
        support_changes = {}
        # Check the delta between the previous and current version.
        # Only store the delta.
        for pid_type in ver_ptype_pid[version]:
            prev_pids = prev_version[pid_type]
            new_pids = ver_ptype_pid[version][pid_type]
            new_support = [pid for pid in new_pids if pid not in prev_pids]
            new_deprecate = [
                f"<span class='deprecated-pid'>{pid}</span>"
                for pid in prev_pids
                if pid not in new_pids
            ]
            new_changes = new_support + new_deprecate
            support_changes[pid_type] = "<br>".join(new_changes)
            prev_version[pid_type] = new_pids

        # Show only versions that have some changes.
        if any(support_changes.values()):
            ver_label = "2.0(1)<br>or older" if version == "2.0(1)" else version
            per_version.append(
                {
                    **{VERSION_KEY: ver_label},
                    **support_changes,
                }
            )
    return per_version


def write_per_pid_markdown(size: str, prefix: str, data_per_pid_type: list[dict]):
    # Create one markdown table for each PID type separately
    md_text = prefix
    for pid_type, data in data_per_pid_type.items():
        md_table = (
            markdown_table(data)
            .set_params(quote=False, row_sep="markdown")
            .get_markdown()
        )
        md_text += f"\n# {pid_type}\n"
        md_text += md_table
        md_text += "\n"

    filename = "index" if size == "newer" else size
    with open(f"docs/{filename}.md", "w") as f:
        f.write(md_text)


def write_chronological_markdown(prefix: str, data: list[dict]):
    # Create one markdown table for all chronological changes
    md_table = (
        markdown_table(data).set_params(quote=False, row_sep="markdown").get_markdown()
    )
    md_text = prefix
    md_text += "\n# Chronological View\n"
    md_text += md_table
    md_text += "\n"
    with open("docs/chrono.md", "w") as f:
        f.write(md_text)


def main():
    """
    Step 1. Create data per PID_type/PID and per version/PID_type
        ptype_ver_pid = {
            "type1": {
                "pid1": ["version1", "version2",...],
                "pid2": ["version2", "version3",...],
            },
            ...
        }
        ver_ptype_pid = {
            "version1": {
                "type1": ["pid1", "pid2",...],
                "type2": ["pid2", "pid3",...],
            },
            ...
        }

    Step 2. Format each data to make tables with py_markdown_table.
        per_pid = {
            "full": {
                "type1": [{
                    PID_KEY: "pid1",
                    "version1": MD_ICON_NOTSUPPORTED,
                    "version2": MD_ICON_NOTSUPPORTED,
                    "version3": MD_ICON_SUPPORTED,
                    ...
                }, {
                    ...
                }],
            },
            "newer": {  # only versions newer than MIN_VER. this will be index.md.
                ...
            }
        }

        per_version = [
            {
                "VERSION_KEY": "version1",
                "type1": "pid1, pid2, pid3(deprecated)",
                "type2": "",
                "type3": "pid1",
            }, {
                "VERSION_KEY": "version2",
                "type1": "pid4"
                "type2": "",
                "type3": "",
            }
        ]
    """
    ptype_ver_pid = {pid_type[0]: defaultdict(list) for pid_type in PID_TYPES}
    ver_ptype_pid = {}

    releases = get_taffy_db(apicrelease_url)

    for release_dict in releases:
        release = release_dict.get("Release", "")
        v = AciVersion(release)

        logging.info(f" --- Release {v} as {v.simple_version} ---")
        if v.simple_version in ver_ptype_pid:
            # There should be no changes in hardware support between patches
            logging.info(f"Release {v.simple_version} was already done. Skip this one")
            continue

        ver_ptype_pid[v.simple_version] = {pid_type[0]: [] for pid_type in PID_TYPES}

        # db_hwSupportXXXX.js needs the version 6.0(4) in the form of 1604
        url = hwsupport_url_prefix + "1" + v.compressed_version + ".js"
        hw_list = get_taffy_db(url)

        for hw in hw_list:
            if hw["ProdType"] in PID_TYPES_TO_IGNORE or hw["ProdID"] in PIDS_TO_IGNORE:
                continue

            pid = hw["ProdID"]
            pid_type = get_pid_type(hw["ProdType"], pid)

            ptype_ver_pid[pid_type][pid].append(v.simple_version)
            ver_ptype_pid[v.simple_version][pid_type].append(pid)

            logging.info(f"[{pid_type}] {pid}: {v.simple_version} supported")

    logging.info(f"Number of versions: {len(ver_ptype_pid)}")
    logging.info(f"Number of PID types: {len(ptype_ver_pid)}")

    # APICs need to be added as supported for older versions
    # for which db_hwSupportXXXX.js does not list APICs at all.
    for pid_type in ptype_ver_pid:
        for pid in ptype_ver_pid[pid_type]:
            if pid not in MIN_SUPPORT_VERSIONS:
                continue
            for version in ver_ptype_pid:
                v = AciVersion(version)
                if not v.older_than(MIN_SUPPORT_VERSIONS[pid]["min"]) and v.older_than(
                    MIN_SUPPORT_VERSIONS[pid]["valid"]
                ):
                    if version not in ptype_ver_pid[pid_type][pid]:
                        ptype_ver_pid[pid_type][pid].append(version)
                    if pid not in ver_ptype_pid[version][pid_type]:
                        ver_ptype_pid[version][pid_type].append(pid)

    # Sort
    for pid_type, pids in ptype_ver_pid.items():
        ptype_ver_pid[pid_type] = dict(sorted(pids.items()))

    ver_ptype_pid = dict(
        sorted(
            ver_ptype_pid.items(),
            key=cmp_to_key(lambda item1, item2: version_sort(item1[0], item2[0])),
        )
    )

    # Create per PID data to convert to markdown table
    per_pid = create_per_pid_data(ptype_ver_pid, all_versions=ver_ptype_pid.keys())

    # Create chronological data to convert to markdown table
    per_version = create_chronological_data(ver_ptype_pid)

    # Create markdown pages with the hide property to
    # hide Navigation and ToC for mkdocs material
    prefix = """---
hide:
  - navigation
  - toc
---

!!! info
    The matrix below are based on data from [Cisco Nexus ACI-Mode Switches Hardware Support Matrix](https://www.cisco.com/c/dam/en/us/td/docs/Website/datacenter/acihwsupport/index.html).

"""  # noqa: E501
    color_note = """
!!! info
    Products that were newly supported from a certain version are shown with plain texts - Ex.) N9K-C9372PX<br>
    Products that were <span class='deprecated-pid'>deprecated</span> from a certain version are shown with colored texts - Ex.) <span class='deprecated-pid'>N9K-C9372PX</span>
"""  # noqa: E501

    for size, data_per_pid_type in per_pid.items():
        write_per_pid_markdown(size, prefix, data_per_pid_type)

    write_chronological_markdown(prefix + color_note, per_version)


if __name__ == "__main__":
    main()
