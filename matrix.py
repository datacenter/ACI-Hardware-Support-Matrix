import logging
import multiline
import re
import requests
from functools import cmp_to_key
from py_markdown_table.markdown_table import markdown_table

hwsupport_url_prefix = "https://www.cisco.com/c/dam/en/us/td/docs/Website/datacenter/acihwsupport/db/db_hwSupport"  # noqa: E501
apicrelease_url = "https://www.cisco.com/c/dam/en/us/td/docs/Website/datacenter/_common/js/db_apicReleases.js"  # noqa: E501

MIN_VER = "4.2(1)"
HW_PID_KEY = "Product Model"

HW_TYPES = [
    # APIC
    ["APIC Servers"],
    # Modular Spine Chassis
    ["Modular Spine Switches", "Spine switch", "Chassis"],
    # Modular Spine Line Cards
    ["Modular Spine Switch Line Cards", "Spine switch module"],
    # Modular Spine Fabric Cards
    ["Modular Spine Switch Fabric Modules", "Spine switch module"],
    # Modular Spine SUP and SC
    ["Modular Spine Switch Supervisor and System Controller Modules", "Switch module"],
    # Modular Leaf Switches
    ["Modular Leaf Switches"],
    # Modular Leaf Line Cards
    ["Modular Leaf Switch Line Cards"],
    # Modular Leaf SUP
    [
        "Modular Leaf Switch Supervisor",
        "Modular Leaf Switch Supervisor and System Controller Modules",
    ],
    # Fixed Spine Switches
    ["Fixed Spine Switches", "Spine switch"],
    # Fixed Leaf Switches
    ["Fixed Leaf Switches", "Top-of-rack (ToR) leaf switch", "Leaf switch"],
]
HW_TYPES_TO_IGNORE = [
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
HW_PIDS = {
    # db_hwSupportXXXX.js may say "Spine switch"
    "Modular Spine Switches": [
        "N9K-C9508-B1",
        "N9K-C9508-B2",
        "N9K-C9516",
    ],
    # db_hwSupportXXXX.js may say "Spine switch"
    "Fixed Spine Switches": [
        "N9K-C9336PQ",
    ],
    # db_hwSupportXXXX.js may say "Spine switch module"
    "Modular Spine Switch Line Cards": [
        "N9K-X9732C-EX",
        "N9K-X9736PQ",
    ],
    # db_hwSupportXXXX.js may say "Spine switch module"
    "Modular Spine Switch Fabric Modules": [
        "N9K-C9504-FM",
        "N9K-C9504-FM-E",
        "N9K-C9508-FM",
        "N9K-C9508-FM-E",
    ],
}
# db_hwSupportXXXX.js for old versions do not list APICs. Hardcoding minimum
# supported version for those. 1.x versions are out of scope.
HW_PIDS_VERSION = {
    "APIC-M1": "2.0(1)",
    "APIC-L1": "2.0(1)",
    "APIC-M2": "2.0(1)",
    "APIC-L2": "2.0(1)",
    "APIC-M3": "4.0(1)",
    "APIC-L3": "4.0(1)",
}
HW_PIDS_TO_IGNORE = [
    "Cisco Nexus 9504"  # this is a duplicate and wrong entry listed as a line card
]


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
            if i == 4 and self.patch:
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


def get_hw_type(hw_type: str, hw_pid: str):
    """Use the same type across versions because each db_hwSupportXXXX.js
    uses a different type name for the same product."""
    for pid_type, pids in HW_PIDS.items():
        if hw_pid in pids:
            return pid_type
    for type_list in HW_TYPES:
        if hw_type in type_list:
            return type_list[0]
    logging.error(f"Unknown HW Type - {hw_type}")
    return hw_type


def main():
    support_per_type = {}
    support_per_type_pid = {}
    support_per_version = {}

    releases = get_taffy_db(apicrelease_url)

    for release_dict in releases:
        release = release_dict.get("Release", "")
        v = AciVersion(release)
        if support_per_version.get(v.simple_version):
            continue

        # DELETE ME
        # if v.simple_version != "6.0(5)" and v.simple_version != "2.0(1)":
        #    continue

        # db_hwSupportXXXX.js needs the version 6.0(4) in the form of 1604
        url = hwsupport_url_prefix + "1" + v.compressed_version + ".js"
        hw_list = get_taffy_db(url)

        support_per_version[v.simple_version] = hw_list
        for hw in hw_list:
            if (
                hw["ProdType"] in HW_TYPES_TO_IGNORE
                or hw["ProdID"] in HW_PIDS_TO_IGNORE
            ):
                continue

            pid = hw["ProdID"]
            pid_type = get_hw_type(hw["ProdType"], pid)

            if not support_per_type_pid.get(pid_type):
                support_per_type_pid[pid_type] = {}
            if not support_per_type_pid[pid_type].get(pid):
                support_per_type_pid[pid_type][pid] = {}

            support_per_type_pid[pid_type][pid][v.simple_version] = MD_ICON_SUPPORTED
            logging.info(f"[{pid_type}] {pid}: {v.simple_version} supported")

    # Add non-supported versions to each PID
    for version in support_per_version:
        for pids_dict in support_per_type_pid.values():
            for pid, pid_data in pids_dict.items():
                if not pid_data.get(version):
                    v = AciVersion(version)
                    if HW_PIDS_VERSION.get(pid) and not v.older_than(
                        HW_PIDS_VERSION[pid]
                    ):
                        pid_data[version] = MD_ICON_SUPPORTED
                    else:
                        pid_data[version] = MD_ICON_NOTSUPPORTED

    # Sort PID types, PIDs and versions for each PID
    support_per_type_pid = dict(sorted(support_per_type_pid.items()))
    for pid_type, pids_dict in support_per_type_pid.items():
        support_per_type[pid_type] = {
            "full": [],
            "index": [],
        }
        pids = dict(sorted(pids_dict.items()))
        for pid, pid_data in pids.items():
            pid_data = dict(
                sorted(
                    pid_data.items(),
                    key=cmp_to_key(lambda v1, v2: version_sort(v1[0], v2[0])),
                )
            )
            min_ver = AciVersion(MIN_VER)
            _pid_data = {
                "full": pid_data,
                "index": {
                    k: v for k, v in pid_data.items() if not min_ver.newer_than(k)
                },
            }
            for size, data in support_per_type[pid_type].items():
                data.append(
                    {
                        **{HW_PID_KEY: pid},
                        **_pid_data[size],
                    }
                )

    # Create markdown tables with the hide property to
    # hide Navigation and ToC for mkdocs material
    text = """---
hide:
  - navigation
  - toc
---

!!! info
    The matrix below are based on data from [Cisco Nexus ACI-Mode Switches Hardware Support Matrix](https://www.cisco.com/c/dam/en/us/td/docs/Website/datacenter/acihwsupport/index.html).

"""  # noqa: E501
    md_texts = {"full": text, "index": text}
    for pid_type, data in support_per_type.items():
        for size in md_texts:
            md_texts[size] += f"\n# {pid_type}\n"
            md_table = (
                markdown_table(data[size])
                .set_params(quote=False, row_sep="markdown")
                .get_markdown()
            )
            md_texts[size] += md_table
            md_texts[size] += "\n"

    for size, md_text in md_texts.items():
        with open(f"docs/{size}.md", "w") as f:
            f.write(md_text)


if __name__ == "__main__":
    main()
