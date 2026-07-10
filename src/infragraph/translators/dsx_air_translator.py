import os
import re
import json

from pathlib import Path
from collections import OrderedDict
from typing import Dict, List, Tuple
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

# Name of the top-level device that composes the whole translated fabric.
FABRIC_DEVICE_NAME = "dsx-air-fabric"


def _role_of(node_name: str) -> str:
    """Derive a node's role (device type) by stripping the trailing index.

    e.g. ``leaf01`` -> ``leaf``, ``spine01`` -> ``spine``, ``server02`` -> ``server``.
    Falls back to the full node name when there is no trailing index.
    """
    role = re.sub(r"[\-_]*\d+$", "", node_name)
    return role or node_name


class DsxAirParser:
    """Parser for DSX Air JSON topology files.

    A DSX Air topology describes a multi-node fabric:

        {
            "title": "Demo",
            "content": {
                "nodes": { "<node>": {"cpu": .., "memory": .., "os": ".."}, ... },
                "links": [ [{"interface": "..", "node": ".."}, {..}], ... ]
            }
        }

    The topology is translated using InfraGraph device composition:

        - Each role (``leaf01``/``leaf02`` -> ``leaf``) becomes its own sub-device
          that contains a ``cpu`` component (count = that node's vCPUs, so the CPUs
          live *inside* each leaf/spine/server) plus a functional component:
          ``switch`` for network operating systems, ``nic`` for hosts.
        - A top-level fabric device composes those sub-devices via ``device``-typed
          components whose count is the number of nodes of that role
          (``leaf`` count 2, ``spine`` count 1, ...).
        - Node-to-node links are collapsed to role-to-role device edges between the
          functional components (e.g. ``leaf.switch`` <-> ``spine.switch``).

    Per-node memory/storage/os and per-interface detail are intentionally dropped.
    """

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

        # node -> role (sub-device / composed-component name)
        self._role_of_node: Dict[str, str] = {}
        # role -> functional component name ("switch" or "nic")
        self._role_func: Dict[str, str] = {}

    def parse(self) -> Infrastructure:
        """Translate the DSX Air topology into an InfraGraph Infrastructure."""
        content = self.data.get("content", {})
        nodes: Dict[str, dict] = content.get("nodes", {})
        links: List[List[dict]] = content.get("links", [])

        title = self.data.get("title", "dsx-air-topology")
        self.infra.name = self.name or title
        self.infra.description = f"DSX Air fabric translated from '{title}'"

        roles = self._group_nodes_by_role(nodes)

        # One composed sub-device per role.
        for role, members in roles.items():
            sub_device, func_name = self._build_role_device(role, members, nodes)
            self.infra.devices.append(sub_device)
            self._role_func[role] = func_name

        # Top-level fabric device that composes the role sub-devices.
        fabric = self._build_fabric_device(roles, links)
        self.infra.devices.append(fabric)
        self.infra.instances.add(name=fabric.name, device=fabric.name, count=1)

        return self.infra

    def _is_switch(self, os_str: str) -> bool:
        os_lower = (os_str or "").lower()
        return any(marker in os_lower for marker in SWITCH_OS_MARKERS)

    def _group_nodes_by_role(self, nodes: Dict[str, dict]) -> "OrderedDict[str, List[str]]":
        """Group node names by role, preserving declaration order."""
        roles: "OrderedDict[str, List[str]]" = OrderedDict()
        for node_name in nodes:
            role = _role_of(node_name)
            roles.setdefault(role, []).append(node_name)
            self._role_of_node[node_name] = role
        return roles

    def _build_role_device(
        self, role: str, members: List[str], nodes: Dict[str, dict]
    ) -> Tuple[Device, str]:
        """Build a sub-device representing a single node of the given role.

        The device holds its own ``cpu`` component (so CPUs are shown inside each
        leaf/spine/server) plus a functional ``switch``/``nic`` component.
        """
        is_switch = any(
            self._is_switch(nodes.get(node, {}).get("os", "")) for node in members
        )

        device = Device()
        device.name = role
        device.description = f"{role} switch" if is_switch else f"{role} host"

        # CPUs live inside each node. Assume homogeneous vCPUs per role and take
        # the representative (first) node's cpu count.
        per_node_cpu = int(nodes.get(members[0], {}).get("cpu", 0) or 0)
        if per_node_cpu > 0:
            cpu = device.components.add(
                name="cpu",
                description="Generic CPU",
                count=per_node_cpu,
            )
            cpu.choice = Component.CPU

        # Functional component that the fabric wires together.
        func_name = "switch" if is_switch else "nic"
        func = device.components.add(
            name=func_name,
            description=f"{role} {func_name}",
            count=1,
        )
        func.choice = Component.SWITCH if is_switch else Component.NIC

        # Internal link so the cpu(s) and the functional component are connected.
        if per_node_cpu > 0:
            internal = device.links.add(
                name="internal",
                description="Internal device interconnect",
            )
            edge = device.edges.add(
                scheme=DeviceEdge.MANY2MANY,
                link=internal.name,
            )
            edge.ep1.component = "cpu"
            edge.ep2.component = func_name
        else:
            # Ensure the required (but unused) links/edges containers are present.
            device.links
            device.edges

        return device, func_name

    def _build_fabric_device(
        self, roles: "OrderedDict[str, List[str]]", links: List[List[dict]]
    ) -> Device:
        """Build the top-level device that composes the role sub-devices."""
        fabric = Device()
        fabric.name = self.name or FABRIC_DEVICE_NAME
        fabric.description = "DSX Air fabric"

        # One device-typed component per role (count = number of nodes).
        for role, members in roles.items():
            component = fabric.components.add(
                name=role,
                description=f"{role} nodes",
                count=len(members),
            )
            component.choice = Component.DEVICE

        self._create_role_edges(fabric, links)

        return fabric

    def _create_role_edges(self, fabric: Device, links: List[List[dict]]):
        """Collapse node links to role-to-role device edges (one per unique pair).

        Endpoints use composed paths (``<role>.<functional component>``) so the
        edge connects the switch/nic inside each composed sub-device.
        """
        seen: Dict[str, object] = {}
        for pair in links:
            role_a = self._role_of_node[pair[0]["node"]]
            role_b = self._role_of_node[pair[1]["node"]]
            key = "-".join(sorted((role_a, role_b)))
            if key in seen:
                continue

            link = fabric.links.add(
                name=key,
                description=f"Connectivity between {role_a} and {role_b}",
            )
            seen[key] = link

            edge = fabric.edges.add(
                scheme=DeviceEdge.MANY2MANY,
                link=link.name,
            )
            edge.ep1.component = f"{role_a}.{self._role_func[role_a]}"
            edge.ep2.component = f"{role_b}.{self._role_func[role_b]}"


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