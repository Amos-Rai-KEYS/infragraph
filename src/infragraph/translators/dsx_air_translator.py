import os
import re
import json

from pathlib import Path
from collections import OrderedDict
from typing import Dict, List
from infragraph import *

# Operating-system markers used to classify a DSX Air node as a network switch.
# Anything that does not match is treated as a host/server.
SWITCH_OS_MARKERS = (
    "cumulus",
    "sonic",
    "eos",
    "nxos",
    "ios",
    "junos",
    "onie",
)

# Name of the single device that represents the whole translated fabric.
FABRIC_DEVICE_NAME = "dsx-air-fabric"


def _role_of(node_name: str) -> str:
    """Derive a node's role (device type) by stripping the trailing index.

    e.g. ``leaf01`` -> ``leaf``, ``spine01`` -> ``spine``, ``server02`` -> ``server``.
    Falls back to the full node name when there is no trailing index.
    """
    role = re.sub(r"[\-_]*\d+$", "", node_name)
    return role or node_name


class DsxAirParser:
    def __init__(self, file_path: str, name: str | None = None):
        _, ext = os.path.splitext(file_path)
        if ext.lower() != ".json":
            raise ValueError(
                f"DsxAirParser expects a JSON file, got '{ext}' instead."
            )
        with open(file_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

        self.name = name
        self.infra = Infrastructure()

        # node -> role (component name)
        self._role_of_node: Dict[str, str] = {}
        # node -> index of that node within its role (declaration order)
        self._index_of_node: Dict[str, int] = {}
        # role-pair key -> the Link connecting them
        self._links: Dict[str, object] = {}

    def parse(self) -> Infrastructure:
        """Translate the DSX Air topology into an InfraGraph Infrastructure."""
        content = self.data.get("content", {})
        nodes: Dict[str, dict] = content.get("nodes", {})
        links: List[List[dict]] = content.get("links", [])

        title = self.data.get("title", "dsx-air-topology")
        self.infra.name = self.name or title
        self.infra.description = f"DSX Air fabric translated from '{title}'"

        device = self._build_device(nodes, links)
        self.infra.devices.append(device)
        self.infra.instances.add(name=device.name, device=device.name, count=1)

        return self.infra

    def _is_switch(self, os_str: str) -> bool:
        os_lower = (os_str or "").lower()
        return any(marker in os_lower for marker in SWITCH_OS_MARKERS)

    def _group_nodes_by_role(self, nodes: Dict[str, dict]) -> "OrderedDict[str, List[str]]":
        """Group node names by role, preserving declaration order."""
        roles: "OrderedDict[str, List[str]]" = OrderedDict()
        for node_name in nodes:
            role = _role_of(node_name)
            members = roles.setdefault(role, [])
            self._role_of_node[node_name] = role
            self._index_of_node[node_name] = len(members)
            members.append(node_name)
        return roles

    def _build_device(self, nodes: Dict[str, dict], links: List[List[dict]]) -> Device:
        """Build the single fabric device with one high-level component per role."""
        device = Device()
        device.name = self.name or FABRIC_DEVICE_NAME
        device.description = "DSX Air fabric"

        # One high-level component per role.
        roles = self._group_nodes_by_role(nodes)
        for role, members in roles.items():
            is_switch = any(
                self._is_switch(nodes.get(node, {}).get("os", "")) for node in members
            )
            component = device.components.add(
                name=role,
                description=f"{role} switch" if is_switch else f"{role} host",
                count=len(members),
            )
            if is_switch:
                component.choice = Component.SWITCH
            else:
                component.choice = Component.CUSTOM
                component.custom.type = "host"

        self._create_role_edges(device, links)

        return device

    def _create_role_edges(self, device: Device, links: List[List[dict]]):
        """Translate node links to indexed instance-to-instance device edges.

        Each DSX Air link is a point-to-point connection between two specific
        nodes, so it becomes a ``one2one`` edge between the exact component
        instances involved (e.g. ``leaf[0]`` <-> ``server[0]``), preserving the
        original wiring instead of collapsing it to a blanket ``many2many``.
        One shared ``Link`` is defined per role pair and reused by its edges.
        """
        for pair in links:
            node_a, node_b = pair[0]["node"], pair[1]["node"]
            role_a, role_b = self._role_of_node[node_a], self._role_of_node[node_b]
            idx_a, idx_b = self._index_of_node[node_a], self._index_of_node[node_b]

            key = "-".join(sorted((role_a, role_b)))
            link = self._links.get(key)
            if link is None:
                link = device.links.add(
                    name=key,
                    description=f"Connectivity between {role_a} and {role_b}",
                )
                self._links[key] = link

            edge = device.edges.add(
                scheme=DeviceEdge.ONE2ONE,
                link=link.name,
            )
            edge.ep1.component = f"{role_a}[{idx_a}]"
            edge.ep2.component = f"{role_b}[{idx_b}]"


def run_dsx_air_parser(
    device_name: str,
    input_file: str = None,
    output_file: str = "infragraph.yaml",
    dump_format: str = "yaml",
) -> str:
    """Parse a DSX Air JSON topology file and export it as an InfraGraph file."""
    if input_file is None or input_file == "":
        raise ValueError(
            "The 'dsx_air' translator requires an input topology file. "
            "Please provide it via the --input option."
        )

    if os.path.isdir(output_file) or output_file.endswith(("/", os.sep)):
        output_file = os.path.join(output_file, f"infragraph.{dump_format.lower()}")

    _, ext = os.path.splitext(output_file)
    ext = ext.lstrip(".").lower()

    if ext != dump_format.lower():
        raise ValueError(
            f"Output extension '.{ext}' does not match format '{dump_format}'."
        )

    if not os.path.isfile(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    parser = DsxAirParser(input_file, device_name)
    infra = parser.parse()

    serialized_data = infra.serialize(dump_format)

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(serialized_data)
        print(f"Translated output written to: {output_file}")

    return serialized_data
