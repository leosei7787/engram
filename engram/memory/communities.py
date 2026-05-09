"""
Memory Communities — Louvain clustering on the knowledge graph.

Reveals associative clusters (e.g. "Location Intelligence org",
"Automotive partnerships"). Clusters are labelled with a local LLM,
displayed as colored regions in the graph explorer, and used for
community-level soft activation in retrieval.
"""
import json
from pathlib import Path
from typing import Callable, Optional


# ─── Lightweight Louvain (no NetworkX dependency) ─────────────────────────
def _build_undirected_weighted(graph: dict) -> dict:
    """
    Project directed graph to undirected, summing weights of parallel edges.
    Returns adj: {node_id: {neighbor_id: weight}}
    """
    adj: dict = {}
    for ed in graph.get("edges", []):
        f, t = ed.get("from"), ed.get("to")
        w = float(ed.get("weight", ed.get("base_strength", 0.5)))
        if not f or not t or f == t:
            continue
        adj.setdefault(f, {}).setdefault(t, 0.0)
        adj.setdefault(t, {}).setdefault(f, 0.0)
        adj[f][t] += w
        adj[t][f] += w

    # Make sure every entity is in adj (isolated nodes too)
    for eid in graph.get("entities", {}):
        adj.setdefault(eid, {})
    return adj


def _modularity_gain(node, target_community, communities, adj, m2):
    """Gain from moving `node` to `target_community`."""
    k_node = sum(adj[node].values())
    sum_in_target = sum(adj[node].get(n, 0) for n in target_community)
    sum_tot_target = sum(sum(adj[n].values()) for n in target_community)
    return sum_in_target - (k_node * sum_tot_target) / m2 if m2 > 0 else 0


def louvain_communities(graph: dict, max_passes: int = 5) -> dict:
    """
    Greedy modularity-based community detection.
    Returns {community_id: [entity_ids]}.

    Note: this is a simplified Louvain — sufficient for ~500-node graphs.
    For larger graphs, replace with python-louvain or networkx.
    """
    adj = _build_undirected_weighted(graph)
    nodes = list(adj.keys())
    if not nodes:
        return {}

    # Each node starts in its own community
    node_comm = {n: i for i, n in enumerate(nodes)}
    m2 = sum(sum(neigh.values()) for neigh in adj.values())  # 2m

    if m2 <= 0:
        return {}

    for _pass in range(max_passes):
        moved = False
        for node in nodes:
            current_comm = node_comm[node]
            # Candidate communities: neighbours' communities
            candidate_comms: dict = {}
            for neighbor, w in adj[node].items():
                cc = node_comm[neighbor]
                candidate_comms[cc] = candidate_comms.get(cc, 0) + w

            best_comm = current_comm
            best_gain = 0.0
            current_members = [n for n, c in node_comm.items() if c == current_comm and n != node]
            for cc, _w in candidate_comms.items():
                if cc == current_comm:
                    continue
                target_members = [n for n, c in node_comm.items() if c == cc]
                gain = _modularity_gain(node, target_members, [], adj, m2)
                if gain > best_gain:
                    best_gain = gain
                    best_comm = cc

            if best_comm != current_comm:
                node_comm[node] = best_comm
                moved = True

        if not moved:
            break

    # Re-pack community IDs to consecutive integers
    unique = sorted(set(node_comm.values()))
    relabel = {old: f"c{i}" for i, old in enumerate(unique)}
    out: dict = {}
    for node, c in node_comm.items():
        cid = relabel[c]
        out.setdefault(cid, []).append(node)
    # Drop singletons (less interesting)
    return {cid: members for cid, members in out.items() if len(members) >= 3}


# ─── Labelling ────────────────────────────────────────────────────────────
LABEL_PROMPT = """You are labelling a community (cluster) of related entities from a CPO's knowledge graph.

Members:
{members}

Generate a SHORT label (max 5 words) and 1 emoji icon.

Output strict JSON:
{{"label": "...", "icon": "🏢"}}

Examples of good labels:
- "Location Intelligence org & leadership"
- "Automotive partnerships & OEM pipeline"
- "Map Foundation & core platform"
- "SDK & certification"

Output ONLY the JSON object."""


DEFAULT_ICONS = ["🏢", "🚗", "🗺️", "🏆", "🤝", "💰", "⚙️", "📊", "🔬", "🎯", "🧭", "📡"]


def label_communities(
    communities: dict,
    graph: dict,
    *,
    ollama_complete: Optional[Callable] = None,
    model: Optional[str] = None,
    yield_for_chat: Optional[Callable] = None,
) -> dict:
    """
    Returns {community_id: {label, icon, members: [eid], size}}
    If ollama_complete is None, uses heuristic labels.
    """
    import re
    out = {}
    entities = graph.get("entities", {})
    for i, (cid, members) in enumerate(communities.items()):
        member_lines = []
        for eid in members[:15]:
            ent = entities.get(eid, {})
            member_lines.append(f"- {ent.get('name', eid)} ({ent.get('type', '?')})")
        member_text = "\n".join(member_lines)

        label = None
        icon = DEFAULT_ICONS[i % len(DEFAULT_ICONS)]

        if ollama_complete and model:
            if yield_for_chat:
                yield_for_chat(max_wait=10, label="community_label")
            try:
                raw = ollama_complete(model, LABEL_PROMPT.format(members=member_text), json_mode=False)
                m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
                if m:
                    parsed = json.loads(m.group())
                    label = (parsed.get("label") or "").strip()
                    icon = (parsed.get("icon") or icon).strip()[:2]
            except Exception:
                pass

        if not label:
            # Heuristic: most common entity type + member count
            types = [entities.get(eid, {}).get("type", "?") for eid in members]
            primary_type = max(set(types), key=types.count) if types else "cluster"
            label = f"{primary_type.title()} cluster ({len(members)} nodes)"

        out[cid] = {
            "label": label,
            "icon":  icon,
            "members": members,
            "size":  len(members),
        }
    return out


# ─── Persistence ──────────────────────────────────────────────────────────
def load_communities(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_communities(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ─── Community-level soft activation ──────────────────────────────────────
def expand_activation_with_communities(
    activated: dict,
    communities: dict,
    *,
    threshold: float = 0.5,
    soft_factor: float = 0.3,
) -> dict:
    """
    If any node in a community is activated above threshold, soft-activate
    all other community members.
    """
    if not activated or not communities:
        return activated

    # Build node → community map
    node_to_comm: dict = {}
    for cid, info in communities.items():
        for m in info.get("members", []):
            node_to_comm[m] = cid

    expanded = dict(activated)
    by_comm: dict = {}
    for node, strength in activated.items():
        cid = node_to_comm.get(node)
        if cid:
            by_comm[cid] = max(by_comm.get(cid, 0), float(strength))

    for cid, comm_strength in by_comm.items():
        if comm_strength < threshold:
            continue
        for member in communities[cid].get("members", []):
            if member not in expanded:
                expanded[member] = comm_strength * soft_factor
    return expanded
