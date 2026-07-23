import os
import json

from pathlib import Path
from typing import Dict, List, Tuple

import yaml

# Default operating systems assigned to exported nodes. InfraGraph carries no OS
# information, so switches/hosts get a sensible placeholder that the forward
# translator would classify the same way.
SWITCH_DEFAULT_OS = "cumulus-vx"
HOST_DEFAULT_OS = "generic/ubuntu"


def _parse_indexed(token: str, count: int) -> Tuple[str, List[int]]:
    """Split an endpoint token into its name and the list of indices it selects.

    Supports plain names (``nic`` -> all indices), single indices (``nic[0]``)
    and Python slice notation (``nic[0:2]``, ``port[1:8:2]``).
    """
    name = token.split("[", 1)[0]
    if "[" not in token:
        return name, list(range(count))

    inside = token[token.index("[") + 1 : token.rindex("]")]
    if ":" in inside:
        parts = inside.split(":")
        start = int(parts[0]) if parts[0] else 0
        stop = int(parts[1]) if len(parts) > 1 and parts[1] else count
        step = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        return name, list(range(start, stop, step))
    return name, [int(inside)]


class InfraGraphToDsxAir:
    """Convert an InfraGraph Infrastructure into a DSX Air topology (fabric only).

    Only the fabric is produced: every device instance becomes a node and every
    ``Infrastructure.edge`` becomes one or more point-to-point node links. Node
    internals (cpu/memory/storage) are intentionally not emitted; each node
    carries just an inferred ``os`` so its role (switch vs host) is recoverable.
    """

    def __init__(self, infra: dict):
        self.infra = infra or {}
        self.devices: Dict[str, dict] = {
            d["name"]: d for d in self.infra.get("devices", [])
        }
        self.instances: Dict[str, dict] = {
            i["name"]: i for i in self.infra.get("instances", [])
        }
        # device name -> {component name: (choice, count)}
        self.components: Dict[str, Dict[str, Tuple[str, int]]] = {
            name: {
                c["name"]: (c.get("choice"), int(c.get("count", 1)))
                for c in device.get("components", [])
            }
            for name, device in self.devices.items()
        }

    # -- role / naming helpers -------------------------------------------------

    def _is_switch(self, device_name: str) -> bool:
        """A device is a switch if it exposes ``port`` components (else a host)."""
        choices = [choice for choice, _ in self.components.get(device_name, {}).values()]
        if any(c == "port" for c in choices):
            return True
        if any(c == "nic" for c in choices):
            return False
        return False

    def _os_for(self, device_name: str) -> str:
        return SWITCH_DEFAULT_OS if self._is_switch(device_name) else HOST_DEFAULT_OS

    def _node_name(self, instance_name: str, index: int) -> str:
        return f"{instance_name}_{index + 1}"

    def _interface(self, device_name: str, comp_name: str, index: int) -> str:
        """Synthesize a DSX interface name from a component index by convention."""
        choice, _ = self.components.get(device_name, {}).get(comp_name, (None, 1))
        if choice == "port":
            return f"swp{index + 1}"
        if choice == "nic":
            return f"eth{index + 1}"
        return f"{comp_name}{index + 1}"

    # -- expansion -------------------------------------------------------------

    def _expand_endpoint(self, endpoint: dict) -> List[Tuple[str, str]]:
        """Expand an edge endpoint into (node, interface) pairs."""
        instance_token = endpoint["instance"]
        component_token = endpoint["component"]

        instance_name = instance_token.split("[", 1)[0]
        instance = self.instances[instance_name]
        device_name = instance["device"]

        _, instance_idxs = _parse_indexed(instance_token, int(instance["count"]))
        _, comp_count = self.components.get(device_name, {}).get(
            component_token.split("[", 1)[0], (None, 1)
        )
        comp_name, comp_idxs = _parse_indexed(component_token, comp_count)

        out: List[Tuple[str, str]] = []
        for di in instance_idxs:
            for ci in comp_idxs:
                out.append(
                    (
                        self._node_name(instance_name, di),
                        self._interface(device_name, comp_name, ci),
                    )
                )
        return out

    def _expand_edge(self, edge: dict) -> List[Tuple[Tuple[str, str], Tuple[str, str]]]:
        """Expand an infrastructure edge into concrete point-to-point link pairs."""
        ep1 = self._expand_endpoint(edge["ep1"])
        ep2 = self._expand_endpoint(edge["ep2"])
        scheme = edge.get("scheme", "one2one")

        pairs: List[Tuple[Tuple[str, str], Tuple[str, str]]] = []
        if scheme == "many2many":
            for a in ep1:
                for b in ep2:
                    pairs.append((a, b))
        else:  # one2one (default)
            for a, b in zip(ep1, ep2):
                pairs.append((a, b))
        return pairs

    # -- build -----------------------------------------------------------------

    def build(self) -> dict:
        """Produce the DSX Air topology dict."""
        nodes: Dict[str, dict] = {}
        for instance in self.instances.values():
            for idx in range(int(instance["count"])):
                node_name = self._node_name(instance["name"], idx)
                nodes[node_name] = {"os": self._os_for(instance["device"])}

        links: List[list] = []
        seen: set = set()
        for edge in self.infra.get("edges", []):
            for (node_a, iface_a), (node_b, iface_b) in self._expand_edge(edge):
                # Deduplicate the same physical link regardless of direction.
                dedup_key = tuple(
                    sorted(((node_a, iface_a), (node_b, iface_b)))
                )
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                links.append(
                    [
                        {"interface": iface_a, "node": node_a},
                        {"interface": iface_b, "node": node_b},
                    ]
                )

        return {
            "format": "JSON",
            "title": self.infra.get("name", "fabric"),
            "ztp": None,
            "content": {"nodes": nodes, "links": links},
        }


def run_dsx_air_export(
    input_file: str,
    output_file: str = "dsx_air.json",
) -> str:
    """Read an InfraGraph YAML/JSON file and export a DSX Air topology JSON file."""
    if input_file is None or input_file == "":
        raise ValueError(
            "The 'dsx_air_export' translator requires an input InfraGraph file. "
            "Please provide it via the --input option."
        )
    if not os.path.isfile(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    with open(input_file, "r", encoding="utf-8") as f:
        infra = yaml.safe_load(f)  # handles both YAML and JSON infragraph files

    if os.path.isdir(output_file) or output_file.endswith(("/", os.sep)):
        output_file = os.path.join(output_file, "dsx_air.json")

    topology = InfraGraphToDsxAir(infra).build()
    serialized = json.dumps(topology, indent=4)

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(serialized)
        print(f"Exported DSX Air topology written to: {output_file}")

    return serialized
