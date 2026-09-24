#!/usr/bin/env python3
"""
ai_chat_archive.py — Universal chat JSON -> HTML converter
Supports: ChatGPT, Claude, Gemini, DeepSeek, Qwen, Grok, and generic formats

──────────────────────────────────────────────────────────────────────────
QUICK SETTINGS  (edit here instead of passing flags every time)
──────────────────────────────────────────────────────────────────────────
"""

# ══════════════════════════════════════════════════════════
#  CONFIG — edit these defaults instead of passing CLI flags
# ══════════════════════════════════════════════════════════

# Directory to scan for JSON files.
# ""  = current working directory (same as running without arguments)
# Example: DEFAULT_DIRECTORY = r"C:\Users\me\Downloads\chat_exports"
DEFAULT_DIRECTORY: str = r".\chat_export"

# Output HTML filename.
DEFAULT_OUTPUT: str = "chat_archive.html"

# Show verbose per-file detection output.
DEFAULT_VERBOSE: bool = False

# Add JSON field hover-tooltips to each message.
DEFAULT_TOOLTIPS: bool = False

# What to do with JSON files whose format cannot be detected:
#   "try"    — attempt best-effort generic parse  (default)
#   "ignore" — silently skip unrecognized files
DEFAULT_ON_UNKNOWN: str = "try"   # "try" | "ignore"


# ══════════════════════════════════════════════════════════
#  HOW TO ADD SUPPORT FOR A NEW CHAT EXPORT FORMAT
# ══════════════════════════════════════════════════════════
"""
Most formats are just "data nested at some path, with messages keyed by
different field names". You usually do NOT need to write parsing code —
just declare paths and field-name aliases on a new PathParser subclass.

Path syntax (colon-separated, used in `chats_path` / `messages_path`):
    "key"      -> step into dict[key]
    "*"        -> expand: if list, take all items; if dict, take all .values()
  Example: "data:*:chat:history:messages:*"
    = go to data, expand list, go to chat.history.messages, expand dict values

Example — adding a new format in 6 lines:

    class MyNewAIParser(PathParser):
        name = "mynewai"
        chats_path        = "conversations:*"
        messages_path     = "turns:*"
        title_fields      = ["title", "subject"]
        role_fields       = ["role", "speaker"]
        content_fields    = ["content", "text"]
        time_fields       = ["timestamp", "created"]
        model_fields      = ["model"]
        id_fields         = ["id"]

Then add `MyNewAIParser()` to `FormatRegistry.PARSERS`. That's it.

For tree-structured formats (ChatGPT/DeepSeek style: messages linked via
parent/children pointers) subclass TreeParser instead — see ChatGPTParser
/ DeepSeekParser below.
"""

import os, sys, json, argparse, re, html as html_mod
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Any


# ══════════════════════════════════════════════════════════
#  DATA MODEL
# ══════════════════════════════════════════════════════════

@dataclass
class Attachment:
    name: str
    mime_type: str = ""
    size: int = 0
    url: str = ""

@dataclass
class Message:
    role: str           # user | assistant | system | tool
    content: str
    timestamp: Optional[float] = None
    attachments: List[Attachment] = field(default_factory=list)
    model: str = ""
    raw_fields: dict = field(default_factory=dict)   # for tooltip

@dataclass
class Chat:
    title: str
    model: str
    messages: List[Message]
    source_file: str
    source_format: str
    created_at: Optional[float] = None
    chat_id: str = ""


# ══════════════════════════════════════════════════════════
#  JSON REPAIR — best-effort fix for common malformed exports
#  (trailing commas, BOM). We never guess at missing brackets;
#  if the structure is too broken, json.loads will still raise
#  and the file is skipped/logged.
# ══════════════════════════════════════════════════════════

def repair_json_text(raw: str) -> str:
    s = raw.lstrip("\ufeff")
    # Remove trailing commas before ] or }  ->  [1,2,] => [1,2]   {"a":1,} => {"a":1}
    s = re.sub(r',(\s*[\]\}])', r'\1', s)
    return s

def load_json_lenient(path: str):
    """Load JSON, retrying with repair on failure. Returns (data, was_repaired)."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    try:
        return json.loads(raw.lstrip("\ufeff")), False
    except Exception:
        pass
    return json.loads(repair_json_text(raw)), True


# ══════════════════════════════════════════════════════════
#  PATH RESOLUTION ENGINE
#  Walks a colon-separated path through nested dict/list structures.
#  "*" expands the current level (list -> items, dict -> .values()).
#  This is what lets new parsers be declared instead of coded.
# ══════════════════════════════════════════════════════════

def resolve_path(data: Any, path: str) -> List[Any]:
    if not path:
        return [data]
    segments = path.split(":")
    current = [data]
    for seg in segments:
        nxt = []
        for node in current:
            if node is None:
                continue
            if seg == "*":
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
                # scalar -> nothing to expand, drop silently
            else:
                if isinstance(node, dict) and seg in node and node[seg] is not None:
                    nxt.append(node[seg])
                elif isinstance(node, list):
                    # tolerate a stray plain key applied to a list: try int index
                    try:
                        idx = int(seg)
                        if 0 <= idx < len(node):
                            nxt.append(node[idx])
                    except ValueError:
                        pass
        current = nxt
    return current


def first_field(node: Any, *field_names: str, default="") -> Any:
    """Return the first present, non-None field from a dict, trying each name."""
    if not isinstance(node, dict):
        return default
    for name in field_names:
        if name in node and node[name] is not None:
            v = node[name]
            if isinstance(v, str) and v.strip() == "":
                continue
            return v
    return default


# ══════════════════════════════════════════════════════════
#  SHARED NORMALIZATION HELPERS
# ══════════════════════════════════════════════════════════

def norm_role(raw) -> str:
    raw = str(raw).lower().strip()
    if raw in ("user", "human", "me"):             return "user"
    if raw in ("assistant","bot","ai","model",
               "gpt","claude","gemini","deepseek",
               "qwen","grok","system_response"):    return "assistant"
    if raw == "system":                             return "system"
    if raw in ("tool","function","plugin"):         return "tool"
    return "assistant"

def norm_content(content: Any) -> str:
    """Recursively flatten content shapes (string / block-list / dict) into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("type", "")
                if t in ("text", ""):
                    parts.append(norm_content(item.get("text") or item.get("content") or item.get("value", "")))
                elif t == "image_url":
                    parts.append("[image]")
                elif t == "tool_use":
                    parts.append(f"[tool: {item.get('name','')}]")
                elif t == "tool_result":
                    parts.append(norm_content(item.get("content", "")))
                else:
                    parts.append(norm_content(item.get("text", "")))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        # ordered preference for nested text-bearing keys
        for k in ("text", "value", "content", "body", "parts"):
            if k in content:
                return norm_content(content[k])
    return str(content)

def norm_timestamp(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return val / 1000 if val > 1e10 else float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # Strip a trailing numeric UTC offset like +08:00 or -05:00
        s2 = re.sub(r'[+-]\d{2}:\d{2}$', '', s)
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(s2.rstrip("Z"), fmt.rstrip("Z")).timestamp()
            except Exception:
                pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None

def extract_attachments(node: dict) -> List[Attachment]:
    out = []
    if not isinstance(node, dict):
        return out
    for key in ("attachments", "files", "documents", "uploads", "file_list"):
        items = node.get(key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = first_field(item, "name", "filename", "file_name", "title", default="file")
                out.append(Attachment(
                    name=name,
                    mime_type=first_field(item, "mime_type", "file_type", "type", "content_type"),
                    size=int(item.get("size") or 0),
                    url=first_field(item, "url", "link", "href"),
                ))
            elif isinstance(item, str):
                out.append(Attachment(name=item))
    return out

def extract_raw_fields(d: dict, max_str: int = 50) -> dict:
    """Flatten a dict's scalar/short fields for the hover-tooltip, truncating long strings."""
    out = {}
    if not isinstance(d, dict):
        return out
    skip = {"content", "text", "parts", "chat_messages", "messages",
            "mapping", "history", "content_list", "fragments", "children"}
    for k, v in d.items():
        if k in skip:
            continue
        if isinstance(v, str):
            out[k] = v[:max_str] + ("…" if len(v) > max_str else "")
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = f"[list, {len(v)} items]"
        elif isinstance(v, dict):
            out[k] = f"{{dict, {len(v)} keys}}"
    return out

def make_chat(title, model, messages, source_file, source_format,
              created_at=None, chat_id="") -> Chat:
    msgs = [m for m in messages if m.content.strip() or m.attachments]
    return Chat(
        title=title or Path(source_file).stem,
        model=model or "unknown",
        messages=msgs,
        source_file=source_file,
        source_format=source_format,
        created_at=created_at,
        chat_id=chat_id,
    )


# ══════════════════════════════════════════════════════════
#  BASE PARSER
#  Two concrete strategies inherit from this:
#    - PathParser : flat or dict-of-messages formats (Qwen, Claude, generic)
#    - TreeParser : parent/children-linked message trees (ChatGPT, DeepSeek)
#  Both expose the same can_parse()/parse() interface to FormatRegistry.
# ══════════════════════════════════════════════════════════

class BaseParser:
    name = "base"

    # ── required by every concrete parser ──
    title_fields: List[str] = ["title", "name"]

    # ── optional field-name candidates (override per-format) ──
    role_fields: List[str] = ["role", "sender", "author", "from"]
    content_fields: List[str] = ["content", "text", "message", "body"]
    time_fields: List[str] = ["timestamp", "created_at", "create_time", "time", "date", "inserted_at"]
    model_fields: List[str] = ["model", "model_slug", "modelName", "engine"]
    id_fields: List[str] = ["id", "uuid", "session_id", "chat_id"]
    created_fields: List[str] = ["created_at", "create_time", "inserted_at", "timestamp"]

    def can_parse(self, data: Any) -> bool:
        raise NotImplementedError

    def parse(self, data: Any, path: str) -> List[Chat]:
        raise NotImplementedError

    # ── shared message-node -> Message conversion ──
    def _node_role(self, node: dict, default="user") -> str:
        return norm_role(first_field(node, *self.role_fields, default=default))

    def _node_content(self, node: dict) -> str:
        return norm_content(first_field(node, *self.content_fields, default=""))

    def _node_time(self, node: dict) -> Optional[float]:
        return norm_timestamp(first_field(node, *self.time_fields, default=None))

    def _node_model(self, node: dict) -> str:
        return first_field(node, *self.model_fields, default="")

    def _node_to_message(self, node: dict, default_role="user") -> Optional[Message]:
        if not isinstance(node, dict):
            return None
        role = self._node_role(node, default=default_role)
        content = self._node_content(node)
        ts = self._node_time(node)
        model = self._node_model(node)
        att = extract_attachments(node)
        return Message(role=role, content=content, timestamp=ts,
                       model=model, attachments=att, raw_fields=extract_raw_fields(node))


# ──────────────────────────────────────────────────────────
#  PathParser — declarative parser for "chats at some path,
#  messages at some path within each chat" shaped formats.
#  Covers: flat message lists, dict-of-messages, list-of-chats, etc.
# ──────────────────────────────────────────────────────────

class PathParser(BaseParser):
    """
    Subclasses declare:
      chats_path     : path (from the JSON root) to each chat object.
                       "" means the root itself is the single chat.
      messages_path  : path (from a chat object) to each message object.
      detect_fields  : list of field names that must ALL exist somewhere
                       along chats_path's first resolved node, used by
                       the default can_parse(). Override can_parse() for
                       trickier detection.
    """
    chats_path: str = ""
    messages_path: str = "messages:*"
    # fields that must be present on a resolved chat-node for can_parse() to match
    detect_required: List[str] = []
    # if true, sort dict-of-messages by timestamp field (Qwen's dict-of-messages case)
    sort_dict_messages_by_time: bool = True

    def _resolve_chat_nodes(self, data: Any) -> List[dict]:
        nodes = resolve_path(data, self.chats_path)
        return [n for n in nodes if isinstance(n, dict)]

    def can_parse(self, data: Any) -> bool:
        nodes = self._resolve_chat_nodes(data)
        if not nodes:
            return False
        sample = nodes[0]
        if self.detect_required and not all(f in sample for f in self.detect_required):
            return False
        msgs = resolve_path(sample, self.messages_path)
        msgs = [m for m in msgs if isinstance(m, dict)]
        if not msgs:
            return False
        # must look like an actual message: has a role-ish or content-ish field
        m0 = msgs[0]
        has_role = any(f in m0 for f in self.role_fields)
        has_content = any(f in m0 for f in self.content_fields)
        return has_role or has_content

    def parse(self, data: Any, path: str) -> List[Chat]:
        chat_nodes = self._resolve_chat_nodes(data)
        out = []
        for chat_node in chat_nodes:
            out.append(self._parse_one_chat(chat_node, path))
        return out

    def _parse_one_chat(self, chat_node: dict, path: str) -> Chat:
        title = first_field(chat_node, *self.title_fields, default="")
        chat_id = first_field(chat_node, *self.id_fields, default="")
        model = first_field(chat_node, *self.model_fields, default="")
        created_at = norm_timestamp(first_field(chat_node, *self.created_fields, default=None))

        msg_nodes = resolve_path(chat_node, self.messages_path)
        # dict.values() resolution loses original ordering guarantee in some Pythons'
        # is fine (3.7+ preserves insertion order) but explicit time-sort is safer
        # when a format interleaves dict-of-messages (e.g. Qwen history.messages).
        msg_dicts = [m for m in msg_nodes if isinstance(m, dict)]
        if self.sort_dict_messages_by_time and self._messages_path_hits_dict(chat_node):
            msg_dicts = sorted(msg_dicts, key=lambda m: self._node_time(m) or 0)

        messages = []
        for node in msg_dicts:
            msg = self._node_to_message(node)
            if msg is None:
                continue
            if not model and msg.model:
                model = msg.model
            messages.append(msg)

        return make_chat(title, model, messages, path, self.name,
                         created_at=created_at, chat_id=chat_id)

    def _messages_path_hits_dict(self, chat_node: dict) -> bool:
        """Check whether the second-to-last path step resolves to a dict
        (meaning we expanded dict.values(), order needs a timestamp sort)."""
        segs = self.messages_path.split(":")
        if not segs or segs[-1] != "*":
            return False
        parent_path = ":".join(segs[:-1])
        parents = resolve_path(chat_node, parent_path)
        return bool(parents) and isinstance(parents[0], dict)


# ──────────────────────────────────────────────────────────
#  TreeParser — for parent/children-linked message trees
#  (ChatGPT "mapping", DeepSeek "mapping"). Subclasses declare
#  the path to the per-chat mapping dict and which fields hold
#  parent-pointer / children-pointer / inline-message data.
# ──────────────────────────────────────────────────────────

class TreeParser(BaseParser):
    """
    Subclasses declare:
      chats_path      : path to each chat object (root if "").
      mapping_field    : key name holding the {node_id: node} tree dict.
      message_field     : key on a tree-node holding the actual message
                            payload (None if the node itself IS the message).
      children_field    : key on a tree-node holding a list of child ids
                            (or, in some exports, a list of inline child nodes).
      parent_field      : key on a tree-node holding the parent id (used
                            as a fallback to find the root when no explicit
                            root marker exists).
    """
    chats_path: str = ""
    mapping_field: str = "mapping"
    message_field: Optional[str] = "message"
    children_field: str = "children"
    parent_field: str = "parent"
    detect_required: List[str] = []

    def _resolve_chat_nodes(self, data: Any) -> List[dict]:
        nodes = resolve_path(data, self.chats_path)
        return [n for n in nodes if isinstance(n, dict)]

    def can_parse(self, data: Any) -> bool:
        nodes = self._resolve_chat_nodes(data)
        if not nodes:
            return False
        sample = nodes[0]
        if self.detect_required and not all(f in sample for f in self.detect_required):
            return False
        mapping = sample.get(self.mapping_field)
        return isinstance(mapping, dict) and len(mapping) > 0

    def parse(self, data: Any, path: str) -> List[Chat]:
        chat_nodes = self._resolve_chat_nodes(data)
        return [self._parse_one_chat(c, path) for c in chat_nodes]

    def _walk_order(self, mapping: dict) -> List[str]:
        """Order node-ids root -> leaf, following the last child at each
        branch point (i.e. the most recently active conversation branch).
        Falls back to parent-pointer reconstruction when no node declares
        a non-empty `children` list (some exports omit it entirely)."""
        has_children_data = any(node.get(self.children_field) for node in mapping.values())

        if has_children_data:
            child_ids = set()
            for node in mapping.values():
                for c in (node.get(self.children_field) or []):
                    cid = c.get("id") if isinstance(c, dict) else c
                    if cid:
                        child_ids.add(cid)
            root_id = next((nid for nid in mapping if nid not in child_ids), None)
            if root_id is None and mapping:
                root_id = next(iter(mapping))

            order, visited = [], set()
            def dfs(nid):
                if nid is None or nid in visited or nid not in mapping:
                    return
                visited.add(nid)
                order.append(nid)
                children = mapping[nid].get(self.children_field) or []
                child_ids_list = [c.get("id") if isinstance(c, dict) else c for c in children]
                if child_ids_list:
                    dfs(child_ids_list[-1])
            if root_id:
                dfs(root_id)
            # pick up any nodes the children-walk missed (disconnected fragments)
            leftover = [nid for nid in mapping if nid not in visited]
            return order + self._order_by_parent_pointers(mapping, leftover)

        # No children data anywhere -> reconstruct via parent pointers only.
        return self._order_by_parent_pointers(mapping, list(mapping.keys()))

    def _order_by_parent_pointers(self, mapping: dict, node_ids: List[str]) -> List[str]:
        """Build node order purely from parent pointers: find chain depth
        for each node by walking up to its root, then sort by (root-distance
        ascending overall via topological pass, then by create_time)."""
        if not node_ids:
            return []
        depth_cache = {}
        def depth(nid, seen=None):
            if nid in depth_cache:
                return depth_cache[nid]
            seen = seen or set()
            if nid in seen or nid not in mapping:
                return 0
            seen.add(nid)
            parent = mapping[nid].get(self.parent_field)
            d = 0 if not parent or parent not in mapping else depth(parent, seen) + 1
            depth_cache[nid] = d
            return d
        # Order by tree depth so parents precede children; ties broken by
        # the message's own timestamp when available (stable chronological order).
        def sort_key(nid):
            node = mapping[nid]
            payload = node.get(self.message_field) if self.message_field else node
            ts = norm_timestamp(first_field(payload or {}, *self.time_fields, default=None)) or 0
            return (depth(nid), ts)
        return sorted(node_ids, key=sort_key)

    def _extract_inline_children_messages(self, mapping: dict) -> List[dict]:
        """Some exports embed the child message directly inside the
        children list instead of referencing it by id (e.g. simplified
        DeepSeek dumps). Collect any such inline message payloads too."""
        inline = []
        for node in mapping.values():
            for c in (node.get(self.children_field) or []):
                if isinstance(c, dict) and self.message_field in c and isinstance(c.get(self.message_field), dict):
                    inline.append(c)
        return inline

    def _parse_one_chat(self, chat_node: dict, path: str) -> Chat:
        title = first_field(chat_node, *self.title_fields, default="")
        chat_id = first_field(chat_node, *self.id_fields, default="")
        created_at = norm_timestamp(first_field(chat_node, *self.created_fields, default=None))
        mapping = chat_node.get(self.mapping_field) or {}

        ordered_ids = self._walk_order(mapping)
        message_payloads = []
        for nid in ordered_ids:
            node = mapping.get(nid) or {}
            payload = node.get(self.message_field) if self.message_field else node
            if isinstance(payload, dict):
                message_payloads.append(payload)

        # Fallback / supplement: nodes whose children carry inline message dicts
        if not message_payloads:
            message_payloads = [c[self.message_field] for c in
                                self._extract_inline_children_messages(mapping)]

        model = ""
        messages = []
        for payload in message_payloads:
            msg = self._build_message(payload)
            if msg is None:
                continue
            if not model and msg.model:
                model = msg.model
            messages.append(msg)

        return make_chat(title, model, messages, path, self.name,
                         created_at=created_at, chat_id=chat_id)

    def _build_message(self, payload: dict) -> Optional[Message]:
        """Default: treat payload like any other message node. Override
        in subclasses with non-trivial content shapes (e.g. DeepSeek's
        'fragments' list, ChatGPT's nested author.role)."""
        return self._node_to_message(payload)


# ══════════════════════════════════════════════════════════
#  CONCRETE FORMAT PARSERS
#  Each one declares paths + field aliases. Override only the
#  bits that genuinely need custom logic (content shape, role
#  derivation from fragment types, etc).
# ══════════════════════════════════════════════════════════

# ──────────────────────────────────────
# Claude  — list of chats, each with chat_messages: [...]
# (also covers the single-chat-dict case via ClaudeSingle)
# ──────────────────────────────────────
class ClaudeParser(PathParser):
    name = "claude"
    chats_path = "*"
    messages_path = "chat_messages:*"
    detect_required = ["chat_messages"]
    title_fields = ["name", "title"]
    role_fields = ["sender", "role"]
    content_fields = ["text", "content"]
    time_fields = ["created_at", "timestamp", "updated_at"]
    id_fields = ["uuid", "id"]

    def _node_content(self, node: dict) -> str:
        # Claude messages carry content as a list of typed blocks; prefer that
        # over the flattened "text" field when present (handles tool blocks).
        blocks = node.get("content")
        if isinstance(blocks, list) and blocks:
            return norm_content(blocks)
        return norm_content(first_field(node, *self.content_fields, default=""))


class ClaudeSingleParser(ClaudeParser):
    name = "claude_single"
    chats_path = ""   # root itself is the chat

    def can_parse(self, data: Any) -> bool:
        return isinstance(data, dict) and "chat_messages" in data and ("uuid" in data or "name" in data)


# ──────────────────────────────────────
# Grok — wrapped in {"conversations": [...]}, otherwise Claude-shaped
# ──────────────────────────────────────
class GrokParser(BaseParser):
    """
    Real Grok export structure:
    {
      conversations: [
        {
          conversation: { id, title, create_time, ... },
          responses: [
            {
              response: { _id, sender, message, model, create_time:{$date:{$numberLong}} }
            }, ...
          ]
        }, ...
      ]
    }
    Each `responses` item is one message (human or assistant).
    role comes from response.sender: "human" or "model".
    """
    name = "grok"
    title_fields = ["title", "name", "summary"]
    time_fields = ["create_time", "created_at"]
    id_fields = ["id"]
    model_fields = ["model"]

    def can_parse(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        convs = data.get("conversations")
        if not isinstance(convs, list) or not convs:
            return False
        first = convs[0]
        return (isinstance(first, dict)
                and "conversation" in first
                and "responses" in first)

    def parse(self, data: Any, path: str) -> List[Chat]:
        out = []
        for item in (data.get("conversations") or []):
            if not isinstance(item, dict):
                continue
            conv_meta = item.get("conversation") or {}
            if not isinstance(conv_meta, dict):
                continue
            title = first_field(conv_meta, "title", "summary", default="")
            chat_id = first_field(conv_meta, "id", default="")
            created_at = self._grok_ts(conv_meta.get("create_time"))
            model = ""
            messages = []
            for resp_wrap in (item.get("responses") or []):
                if not isinstance(resp_wrap, dict):
                    continue
                resp = resp_wrap.get("response") or {}
                if not isinstance(resp, dict):
                    continue
                sender = first_field(resp, "sender", default="")
                role = norm_role(sender) if sender else "assistant"
                content = first_field(resp, "message", "content", "text", default="")
                ts = self._grok_ts(resp.get("create_time"))
                msg_model = first_field(resp, "model", default="")
                if msg_model and not model:
                    model = msg_model
                if not content.strip():
                    continue
                messages.append(Message(
                    role=role, content=content, timestamp=ts,
                    model=msg_model, raw_fields=extract_raw_fields(resp)
                ))
            out.append(make_chat(title, model, messages, path, "grok",
                                 created_at=created_at, chat_id=chat_id))
        return out

    def _grok_ts(self, val) -> Optional[float]:
        """Grok timestamps may be ISO strings or MongoDB-style {$date:{$numberLong:ms}}"""
        if val is None:
            return None
        if isinstance(val, dict):
            date = val.get("$date") or {}
            if isinstance(date, dict):
                nl = date.get("$numberLong")
                if nl is not None:
                    try:
                        return int(nl) / 1000
                    except Exception:
                        pass
            if isinstance(date, (int, float)):
                return date / 1000 if date > 1e10 else float(date)
        return norm_timestamp(val)


# ──────────────────────────────────────
# Qwen — {"data": [ { chat: { history: { messages: {id: msg} } } } ]}
# Messages are a DICT keyed by message-id, not a list -> needs time-sort.
# ──────────────────────────────────────
class QwenParser(PathParser):
    name = "qwen"
    chats_path = "data:*"
    messages_path = "chat:history:messages:*"
    detect_required = ["chat"]
    title_fields = ["title", "name"]
    role_fields = ["role"]
    content_fields = ["content"]
    time_fields = ["timestamp", "created_at"]
    model_fields = ["model", "modelName"]
    id_fields = ["id"]
    created_fields = ["created_at"]

    def can_parse(self, data: Any) -> bool:
        nodes = self._resolve_chat_nodes(data)
        if not nodes:
            return False
        sample = nodes[0]
        chat = sample.get("chat")
        if not isinstance(chat, dict):
            return False
        hist = (chat.get("history") or {}).get("messages")
        return isinstance(hist, dict) and len(hist) > 0

    def _node_content(self, node: dict) -> str:
        # Qwen assistant messages often carry the real text inside
        # content_list[{phase: "answer"/"thinking_summary"}] rather than
        # the top-level "content" field (which can be empty).
        content_list = node.get("content_list")
        if isinstance(content_list, list) and content_list:
            think_parts, answer_parts = [], []
            for cl in content_list:
                if not isinstance(cl, dict):
                    continue
                phase = cl.get("phase", "")
                text = cl.get("content", "")
                if phase == "thinking_summary":
                    extra = cl.get("extra") or {}
                    thought = (extra.get("summary_thought") or {}).get("content", [])
                    if isinstance(thought, list):
                        text = " ".join(thought)
                    if text:
                        think_parts.append(text)
                elif phase == "answer":
                    if text:
                        answer_parts.append(text)
            parts = []
            if think_parts:
                parts.append("<think>\n" + "\n".join(think_parts) + "\n</think>")
            parts.extend(answer_parts)
            if parts:
                return "\n".join(parts)
        return norm_content(first_field(node, *self.content_fields, default=""))


# ──────────────────────────────────────
# Generic — {"messages"|"history"|"thread"|"chat": [ {role, content} ]}
# Broad catch-all; tried after specific formats fail.
# ──────────────────────────────────────
class GenericParser(PathParser):
    name = "generic"
    chats_path = ""
    title_fields = ["title", "name", "subject"]
    role_fields = ["role", "author", "sender", "type", "from"]
    content_fields = ["content", "text", "message", "body", "value"]
    time_fields = ["timestamp", "created_at", "create_time", "time", "date"]
    model_fields = ["model", "engine", "assistant_id"]

    def __init__(self):
        self._matched_key = None

    def can_parse(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        for key in ("messages", "history", "thread", "chat"):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                if any(f in v[0] for f in self.role_fields):
                    self._matched_key = key
                    return True
            if isinstance(v, dict) and v:
                sample = next(iter(v.values()))
                if isinstance(sample, dict) and any(f in sample for f in self.role_fields):
                    self._matched_key = key
                    return True
        return False

    def parse(self, data: Any, path: str) -> List[Chat]:
        key = self._matched_key or "messages"
        self.messages_path = f"{key}:*"
        return super().parse(data, path)


# ──────────────────────────────────────
# Gemini/Bard — {"messages": [ {role, parts: [{text}]} ]}
# ──────────────────────────────────────
class GeminiParser(PathParser):
    name = "gemini"
    chats_path = ""
    messages_path = "messages:*"
    title_fields = ["title", "name"]
    role_fields = ["role", "author"]
    model_fields = ["model", "modelVersion"]
    time_fields = ["timestamp", "createTime", "create_time"]

    def can_parse(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        msgs = data.get("messages")
        return isinstance(msgs, list) and bool(msgs) and isinstance(msgs[0], dict) and "parts" in msgs[0]

    def _node_content(self, node: dict) -> str:
        parts = node.get("parts") or []
        if isinstance(parts, list):
            return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in parts)
        return ""


# ──────────────────────────────────────
# Google "My Activity" — Gemini Apps / AI Mode records:
#   [{ header, title, time, products, details:[{url}],
#      safeHtmlItem: [{ html: "<p>Ваш запрос:...</p><p>Ответ Поиска:...</p>" }] }]
# ──────────────────────────────────────
class GeminiActivityParser(BaseParser):
    name = "gemini_activity"
    title_fields = ["title"]
    time_fields = ["time"]
    id_fields = ["titleUrl", "url"]

    def can_parse(self, data: Any) -> bool:
        records = data if isinstance(data, list) else [data]
        if not records:
            return False
        for rec in records:
            if not isinstance(rec, dict):
                return False
            if "title" not in rec:
                continue
            items = rec.get("safeHtmlItem")
            if (isinstance(items, list) and items and isinstance(items[0], dict)
                    and isinstance(items[0].get("html"), str)):
                return True
        return False

    def parse(self, data: Any, path: str) -> List[Chat]:
        records = data if isinstance(data, list) else [data]
        out: List[Chat] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            chat = self._parse_one(rec, path)
            if chat is not None:
                out.append(chat)
        return out

    def _parse_one(self, rec: dict, path: str) -> Optional[Chat]:
        title_raw = first_field(rec, *self.title_fields, default="")
        header = first_field(rec, "header", "product", default="")
        ts = norm_timestamp(first_field(rec, *self.time_fields, default=None))
        products = rec.get("products") or []
        model = products[0] if products and isinstance(products[0], str) else "unknown"

        chat_id = ""
        details = rec.get("details") or []
        if details and isinstance(details[0], dict):
            chat_id = str(first_field(details[0], "url", "name", default=""))
        if not chat_id:
            chat_id = first_field(rec, "titleUrl", default="")

        html = ""
        items = rec.get("safeHtmlItem") or []
        if items and isinstance(items[0], dict):
            html = first_field(items[0], "html", default="")
        query, answer = self._split_items(html, title_raw)

        messages: List[Message] = []
        if query.strip():
            messages.append(Message(role="user", content=query, timestamp=ts))
        if answer.strip():
            messages.append(Message(role="assistant", content=answer, timestamp=ts, model=model))
        if not messages and title_raw.strip():
            messages.append(Message(role="user", content=title_raw, timestamp=ts))

        short = title_raw[:80]
        title = (f"{header}: {short}" if header else short)
        if len(title_raw) > 80:
            title += "…"
        return make_chat(title, model, messages, path, self.name,
                         created_at=ts, chat_id=chat_id)

    def _split_items(self, html: str, fallback: str):
        text = self._html_to_text(html)
        m = re.search(r"Ваш запрос:\s*(.*?)(?:\s*Ответ Поиска:\s*(.*))?$", text, re.S)
        if m and (m.group(1) or m.group(2)):
            return m.group(1).strip(), (m.group(2) or "").strip()
        return fallback, text

    @staticmethod
    def _html_to_text(html: str) -> str:
        s = re.sub(r"(?i)<br\s*/?>", "\n", html)
        s = re.sub(r"(?i)</(?:p|li|h\d|div|tr)>", "\n", s)
        s = re.sub(r"\s*<[^>]*>\s*", " ", s)
        s = html_mod.unescape(s)
        s = re.sub(r"(?<=/)\s+(?=\S)", "", s)
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()


# ──────────────────────────────────────
# Raw top-level list of messages  [ {role, content}, ... ]
# ──────────────────────────────────────
class RawMessageListParser(PathParser):
    name = "raw_list"
    chats_path = ""  # special-cased below: root list IS the messages
    messages_path = ""

    def can_parse(self, data: Any) -> bool:
        if not isinstance(data, list) or not data:
            return False
        first = data[0]
        return (isinstance(first, dict)
                and any(f in first for f in self.role_fields)
                and any(f in first for f in self.content_fields))

    def parse(self, data: Any, path: str) -> List[Chat]:
        messages, model = [], ""
        for node in data:
            if not isinstance(node, dict):
                continue
            msg = self._node_to_message(node)
            if msg is None:
                continue
            if not model and msg.model:
                model = msg.model
            messages.append(msg)
        return [make_chat(Path(path).stem, model, messages, path, self.name)]


# ──────────────────────────────────────
# ChatGPT — list-of-chats or single-chat, each with a "mapping" tree
# of {node_id: {message, parent, children}}.
# ──────────────────────────────────────
class ChatGPTParser(TreeParser):
    name = "chatgpt"
    chats_path = "*"
    mapping_field = "mapping"
    message_field = "message"
    children_field = "children"
    title_fields = ["title"]
    time_fields = ["create_time"]
    created_fields = ["create_time"]
    detect_required = ["mapping", "title"]

    def can_parse(self, data: Any) -> bool:
        if isinstance(data, dict) and "mapping" in data and "title" in data:
            # single chat dict — handled by ChatGPTSingleParser, not us
            return False
        if not super().can_parse(data):
            return False
        # Distinguish from other mapping-tree formats (e.g. DeepSeek) by
        # checking the message payload shape: ChatGPT nests author.role
        # and content.content_type/parts, not a flat "fragments" list.
        nodes = self._resolve_chat_nodes(data)
        if not nodes:
            return False
        mapping = nodes[0].get(self.mapping_field) or {}
        for node in mapping.values():
            payload = node.get(self.message_field)
            if isinstance(payload, dict):
                if "fragments" in payload:
                    return False  # DeepSeek-shaped, not us
                if "author" in payload or "content" in payload:
                    return True
        return False


class ChatGPTSingleParser(ChatGPTParser):
    name = "chatgpt_single"
    chats_path = ""

    def can_parse(self, data: Any) -> bool:
        if not (isinstance(data, dict) and "mapping" in data and "title" in data):
            return False
        mapping = data.get("mapping") or {}
        for node in mapping.values():
            payload = node.get(self.message_field) if isinstance(node, dict) else None
            if isinstance(payload, dict):
                if "fragments" in payload:
                    return False  # DeepSeek-shaped
                if "author" in payload or "content" in payload:
                    return True
        return False


class ChatGPTConversationsKeyParser(ChatGPTParser):
    name = "chatgpt_conversations"
    chats_path = "conversations:*"

    def can_parse(self, data: Any) -> bool:
        return isinstance(data, dict) and isinstance(data.get("conversations"), list) and super().can_parse({"_": data.get("conversations")}) is False and bool(self._resolve_chat_nodes(data))

    def _resolve_chat_nodes(self, data: Any) -> List[dict]:
        nodes = resolve_path(data, self.chats_path)
        return [n for n in nodes if isinstance(n, dict) and "mapping" in n]


# Shared author.role / content.parts shape for ChatGPT message payloads
class ChatGPTParser(ChatGPTParser):  # noqa: F811 - intentional reopen to add _build_message
    def _build_message(self, payload: dict) -> Optional[Message]:
        author = payload.get("author") or {}
        role_raw = first_field(author, "role", default="")
        if not role_raw or role_raw == "system":
            return None
        role = norm_role(role_raw)
        c_obj = payload.get("content") or {}
        c_type = c_obj.get("content_type", "text") if isinstance(c_obj, dict) else "text"
        if c_type in ("text", "multimodal_text"):
            text = norm_content(c_obj.get("parts", []) if isinstance(c_obj, dict) else [])
        else:
            text = norm_content(c_obj)
        ts = norm_timestamp(payload.get("create_time"))
        meta = payload.get("metadata") or {}
        model = first_field(meta, "model_slug", "model", default="")
        return Message(role=role, content=text, timestamp=ts, model=model,
                       attachments=extract_attachments(payload),
                       raw_fields=extract_raw_fields(payload))


# ──────────────────────────────────────
# DeepSeek — "mapping" tree, but each node's message holds a
# "fragments" list (REQUEST / THINK / TOOL_* / RESPONSE) instead
# of plain text, and children may be id-strings OR inline objects.
# ──────────────────────────────────────
class DeepSeekParser(TreeParser):
    """
    Real DeepSeek export structure:
    [ {
        id, title, inserted_at, updated_at,
        mapping: {
          "root": { id, parent: null, children: ["1"], message: null },
          "1":    { id, parent: "root", children: ["2"], message: { files, model, inserted_at, fragments: [{type, content}] } },
          "2":    { id, parent: "1",    children: ["3"], message: { ... } },
          ...
        }
      }, ... ]

    children is a list of STRING ids (not inline objects).
    Fragment types: REQUEST (user), THINK (assistant thinking),
                    RESPONSE (assistant answer), TOOL_SEARCH, TOOL_OPEN (tool, skip or show).
    """
    name = "deepseek"
    chats_path = "*"
    mapping_field = "mapping"
    message_field = "message"
    children_field = "children"
    parent_field = "parent"
    title_fields = ["title", "name"]
    time_fields = ["inserted_at", "created_at"]
    created_fields = ["inserted_at", "created_at"]
    id_fields = ["id", "session_id"]
    model_fields = ["model"]

    def can_parse(self, data: Any) -> bool:
        nodes = self._resolve_chat_nodes(data)
        if not nodes:
            return False
        sample = nodes[0]
        mapping = sample.get(self.mapping_field)
        if not isinstance(mapping, dict) or not mapping:
            return False
        # Must have at least one node with fragments in its message
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            msg = node.get(self.message_field)
            if isinstance(msg, dict) and "fragments" in msg:
                return True
        return False

    def _walk_order(self, mapping: dict) -> List[str]:
        """Walk from root, following children (list of string IDs)."""
        # Find root: parent is None or "root" string, or not in mapping as a child
        child_ids = set()
        for node in mapping.values():
            if isinstance(node, dict):
                for c in (node.get(self.children_field) or []):
                    if isinstance(c, str):
                        child_ids.add(c)
        # "root" node has parent=None
        root_id = next(
            (nid for nid, node in mapping.items()
             if isinstance(node, dict) and node.get(self.parent_field) is None),
            None
        )
        if root_id is None:
            root_id = next((nid for nid in mapping if nid not in child_ids), None)
        if root_id is None and mapping:
            root_id = next(iter(mapping))

        order, visited = [], set()
        def dfs(nid):
            if nid is None or nid in visited or nid not in mapping:
                return
            visited.add(nid)
            order.append(nid)
            children = mapping[nid].get(self.children_field) or []
            # children are string IDs
            child_strs = [c for c in children if isinstance(c, str)]
            if child_strs:
                dfs(child_strs[-1])  # take last = most recent branch
        if root_id:
            dfs(root_id)
        # pick up any disconnected nodes
        for nid in mapping:
            if nid not in visited:
                order.append(nid)
        return order

    def _build_message(self, payload: dict) -> Optional[Message]:
        if not isinstance(payload, dict):
            return None
        fragments = payload.get("fragments") or []
        if not fragments:
            return None
        model = first_field(payload, "model", default="deepseek")
        ts = norm_timestamp(first_field(payload, "inserted_at", "created_at", default=None))
        att = extract_attachments(payload)

        types = {f.get("type", "") for f in fragments if isinstance(f, dict)}
        if "REQUEST" in types:
            role = "user"
            text = "\n".join(f.get("content", "") for f in fragments
                             if isinstance(f, dict) and f.get("type") == "REQUEST")
        else:
            role = "assistant"
            think_parts = [f.get("content", "") for f in fragments
                           if isinstance(f, dict) and f.get("type") == "THINK"]
            resp_parts  = [f.get("content", "") for f in fragments
                           if isinstance(f, dict) and f.get("type") == "RESPONSE"]
            tool_parts  = [f"{f.get('type','')}:\n{f.get('content','')}" for f in fragments
                           if isinstance(f, dict) and f.get("type","").startswith("TOOL_")]
            parts = []
            if think_parts:
                parts.append("<think>\n" + "\n".join(think_parts) + "\n</think>")
            parts.extend(resp_parts)
            parts.extend(tool_parts)
            text = "\n".join(p for p in parts if p)

        if not text.strip() and not att:
            return None
        return Message(role=role, content=text, timestamp=ts, model=model,
                       attachments=att, raw_fields=extract_raw_fields(payload))


# ══════════════════════════════════════════════════════════
#  FORMAT REGISTRY
#  Parsers are tried in order; first .can_parse() match wins.
#  To add a new format: write a PathParser/TreeParser subclass
#  above (see module docstring) and append an instance here.
# ══════════════════════════════════════════════════════════

class FormatRegistry:
    PARSERS: List[BaseParser] = [
        ClaudeSingleParser(),
        ClaudeParser(),
        GrokParser(),
        ChatGPTSingleParser(),
        ChatGPTConversationsKeyParser(),
        ChatGPTParser(),
        DeepSeekParser(),
        QwenParser(),
        GeminiParser(),
        GeminiActivityParser(),
        GenericParser(),
        RawMessageListParser(),
    ]

    @classmethod
    def detect(cls, data: Any) -> Optional[BaseParser]:
        for p in cls.PARSERS:
            try:
                if p.can_parse(data):
                    return p
            except Exception:
                pass
        return None

    @classmethod
    def parse(cls, data: Any, path: str, on_unknown: str = "try") -> List[Chat]:
        parser = cls.detect(data)
        if parser:
            try:
                chats = parser.parse(data, path)
                if chats:
                    return chats
            except Exception as e:
                print(f"  [WARN] Parser {parser.name} failed on {path}: {e}", file=sys.stderr)

        if on_unknown == "ignore":
            return []

        # best-effort fallback
        for p in (GenericParser(), RawMessageListParser()):
            try:
                chats = p.parse(data, path) if p.can_parse(data) else []
                if chats and any(c.messages for c in chats):
                    return chats
            except Exception:
                pass
        return []


# ══════════════════════════════════════════════════════════
#  FILE COLLECTION
# ══════════════════════════════════════════════════════════

def collect_json_files(root: str) -> List[str]:
    result = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(".json"):
                result.append(os.path.join(dirpath, fn))
    result.sort()
    return result


# ══════════════════════════════════════════════════════════
#  HTML RENDERING
# ══════════════════════════════════════════════════════════

def fmt_time(ts: Optional[float]) -> str:
    if ts is None: return ""
    try:    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except: return ""

def esc(s) -> str:
    return html_mod.escape(str(s) if s is not None else "")

def render_message_html(msg: Message, idx: int, show_tooltip: bool, fallback_model: str = "") -> str:
    role_class = {"user":"msg-user","assistant":"msg-assistant",
                  "system":"msg-system","tool":"msg-tool"}.get(msg.role,"msg-assistant")
    role_label = {"user":"User","assistant":"Assistant",
                  "system":"System","tool":"Tool"}.get(msg.role, msg.role.capitalize())

    time_html = (f'<span class="msg-time">{esc(fmt_time(msg.timestamp))}</span>'
                 if msg.timestamp else "")
    model_html = (f'<span class="msg-model-tag">{esc(msg.model)}</span>'
                  if msg.model else "")
    
    eff_model = msg.model or fallback_model

    # Render content: handle <think> blocks, code blocks, newlines
    raw = msg.content
    parts_out = []
    last = 0

    # <think>...</think>
    for m in re.finditer(r'<think>(.*?)</think>', raw, re.DOTALL):
        before = esc(raw[last:m.start()]).replace("\n","<br>")
        parts_out.append(before)
        think_text = esc(m.group(1).strip())
        parts_out.append(
            f'<details class="think-block"><summary>🧠 Thinking</summary>'
            f'<div class="think-content">{think_text.replace(chr(10),"<br>")}</div></details>')
        last = m.end()
    remaining = raw[last:]

    # code blocks in remaining
    sub_parts = []
    sub_last = 0
    for m in re.finditer(r'```(\w*)\n?(.*?)```', remaining, re.DOTALL):
        before = esc(remaining[sub_last:m.start()]).replace("\n","<br>")
        sub_parts.append(before)
        lang = esc(m.group(1) or "")
        code = esc(m.group(2))
        sub_parts.append(f'<pre><code class="lang-{lang}">{code}</code></pre>')
        sub_last = m.end()
    tail = esc(remaining[sub_last:]).replace("\n","<br>")
    sub_parts.append(tail)
    parts_out.extend(sub_parts)
    content_html = "".join(parts_out)

    # Attachments
    attach_html = ""
    if msg.attachments:
        badges = "".join(
            f'<span class="attach-badge">📎 {esc(a.name)}'
            + (f' ({a.size//1024} KB)' if a.size > 1024 else "")
            + '</span>'
            for a in msg.attachments)
        attach_html = f'<div class="msg-attachments">{badges}</div>'

    # Tooltip
    tooltip_html = ""
    if show_tooltip and msg.raw_fields:
        rows = "".join(
            f'<tr><td class="tt-key">{esc(k)}</td><td class="tt-val">{esc(str(v))}</td></tr>'
            for k, v in msg.raw_fields.items())
        tooltip_html = f'<div class="msg-tooltip"><table>{rows}</table></div>'

    tooltip_class = " has-tooltip" if show_tooltip and msg.raw_fields else ""

    return (f'<div class="message {role_class}{tooltip_class}" '
            f'data-idx="{idx}" data-role="{msg.role}" data-model="{esc(eff_model)}">'
            f'<div class="msg-header">'
            f'<span class="msg-role">{role_label}</span>'
            f'{model_html}{time_html}'
            f'</div>'
            f'<div class="msg-body search-content">{content_html}</div>'
            f'{attach_html}{tooltip_html}'
            f'</div>')


def chat_to_search_entry(chat: Chat, i: int) -> dict:
    full_text = chat.title + " " + chat.model
    msgs = []
    for msg in chat.messages:
        content = msg.content or ""
        full_text += " " + content
        eff_model = msg.model or chat.model
        msgs.append({"r": msg.role or "assistant", "t": content.lower(), "m": eff_model})
    return {"i": i, "title": chat.title, "model": chat.model,
            "text": full_text, "src": os.path.basename(chat.source_file),
            "fmt": chat.source_format, "msgs": msgs}


def render_html(chats: List[Chat], source_root: str, show_tooltip: bool, output_filename: str = "chat_archive.html") -> tuple[str, str]:
    # ── sidebar
    sidebar_items = []
    for i, chat in enumerate(chats):
        ts = fmt_time(chat.created_at) if chat.created_at else ""
        n = len(chat.messages)
        sidebar_items.append(
            f'<div class="sidebar-item" data-idx="{i}" onclick="jumpToChat({i})">'
            f'<div class="si-title">{esc(chat.title)}</div>'
            f'<div class="si-meta">{esc(chat.model)} · {n} msgs'
            + (f' · {ts}' if ts else "")
            + f'</div></div>')

    # ── chat sections
    chat_sections = []
    for i, chat in enumerate(chats):
        ts = fmt_time(chat.created_at) if chat.created_at else ""
        src = os.path.basename(chat.source_file)
        msgs_html = "\n".join(render_message_html(m, j, show_tooltip, fallback_model=chat.model)
                               for j, m in enumerate(chat.messages))
        chat_sections.append(
            f'<section class="chat-section" id="chat-{i}" data-idx="{i}">'
            f'<div class="chat-header">'
            f'<div class="chat-title-row">'
            f'<h2 class="chat-title">{esc(chat.title)}</h2>'
            f'<span class="chat-badge">{esc(chat.source_format)}</span>'
            f'</div>'
            f'<div class="chat-meta">'
            f'<span>🤖 {esc(chat.model)}</span>'
            f'<span>💬 {len(chat.messages)} messages</span>'
            + (f'<span>📅 {esc(ts)}</span>' if ts else "")
            + f'<span class="chat-src" title="{esc(chat.source_file)}">📄 {esc(src)}</span>'
            f'</div></div>'
            f'<div class="messages-container">{msgs_html}</div>'
            f'</section>')

    # Build search index data (for external JS file)
    search_index = [chat_to_search_entry(c, i) for i, c in enumerate(chats)]
    search_js = "window.SEARCH_INDEX = " + json.dumps(search_index, ensure_ascii=True) + ";"
    total_msgs = sum(len(c.messages) for c in chats)
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sidebar_html = "\n".join(sidebar_items)
    chats_html = "\n".join(chat_sections)

    # ─────────────────────────────────────────────────────────
    # NOTE: Python f-string uses {{ }} for literal braces.
    # JS uses { } normally inside the template.
    # ─────────────────────────────────────────────────────────
    html = """<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chat Archive</title>
<style>
/* ── Theme tokens ── */
[data-theme="light"] {
  --bg: #f5f5f0;
  --bg2: #ffffff;
  --bg3: #f0f0eb;
  --bg4: #e8e8e2;
  --border: #d8d8d0;
  --text: #1a1a1a;
  --text2: #555550;
  --text3: #999990;
  --accent: #5b4de0;
  --accent2: #4a3cc0;
  --accent-fg: #ffffff;
  --user-bg: #eef0ff;
  --user-border: #c0c8f8;
  --ai-bg: #f0faf0;
  --ai-border: #b8e8b8;
  --system-bg: #fffbee;
  --system-border: #e8d888;
  --tool-bg: #fff5ee;
  --tool-border: #f0c898;
  --think-bg: #f8f0ff;
  --think-border: #d0b8f0;
  --code-bg: #f8f8f4;
  --hl-bg: rgba(255,200,0,0.35);
  --hl-color: #7a5800;
  --hl-cur-bg: rgba(255,100,0,0.35);
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
}
[data-theme="dark"] {
  --bg: #0f0f13;
  --bg2: #16161d;
  --bg3: #1e1e28;
  --bg4: #252533;
  --border: #2e2e3e;
  --text: #e2e2f0;
  --text2: #9090b0;
  --text3: #5a5a7a;
  --accent: #7c6af7;
  --accent2: #5e4ed6;
  --accent-fg: #ffffff;
  --user-bg: #1a1a2e;
  --user-border: #2e2e5e;
  --ai-bg: #141420;
  --ai-border: #2a2a40;
  --system-bg: #1a1f1a;
  --system-border: #2a3a2a;
  --tool-bg: #1f1a1a;
  --tool-border: #3a2a2a;
  --think-bg: #1a1428;
  --think-border: #3a2a5a;
  --code-bg: #0a0a10;
  --hl-bg: rgba(247,215,106,0.25);
  --hl-color: #f7d76a;
  --hl-cur-bg: rgba(247,136,106,0.4);
  --shadow: 0 1px 4px rgba(0,0,0,0.4);
}

*,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
html { scroll-behavior:smooth; }
body {
  background:var(--bg);
  color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-size:14px;
  line-height:1.6;
  display:flex;
  min-height:100vh;
}

/* ── Sidebar ── */
#sidebar {
  width:290px;
  background:var(--bg2);
  border-right:1px solid var(--border);
  display:flex;
  flex-direction:column;
  position:fixed;
  top:0;left:0;bottom:0;
  z-index:100;
}
#sidebar-header {
  padding:14px 16px 12px;
  border-bottom:1px solid var(--border);
}
#sidebar-header h1 {
  font-size:14px;
  font-weight:700;
  color:var(--accent);
  margin-bottom:2px;
}
#sidebar-stats { font-size:11px; color:var(--text3); }
#sidebar-search { padding:10px; border-bottom:1px solid var(--border); }
#sidebar-filter {
  width:100%;
  background:var(--bg3);
  border:1px solid var(--border);
  border-radius:6px;
  color:var(--text);
  padding:6px 10px;
  font-size:12px;
  outline:none;
}
#sidebar-filter:focus { border-color:var(--accent); }
#sidebar-list { flex:1; overflow-y:auto; padding:6px; }
#sidebar-list::-webkit-scrollbar { width:4px; }
#sidebar-list::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }
.sidebar-item {
  padding:8px 10px;
  border-radius:6px;
  cursor:pointer;
  margin-bottom:2px;
  border:1px solid transparent;
  transition:background 0.12s;
}
.sidebar-item:hover { background:var(--bg3); }
.sidebar-item.active { background:var(--bg4); border-color:var(--accent); }
.sidebar-item.hidden { display:none; }
.si-title { font-size:12px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.si-meta { font-size:10px; color:var(--text3); margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

/* ── Main ── */
#main { margin-left:290px; flex:1; display:flex; flex-direction:column; }

/* ── Toolbar ── */
#toolbar {
  position:sticky;
  top:0;
  z-index:50;
  background:var(--bg2);
  border-bottom:1px solid var(--border);
  padding:8px 16px;
  display:flex;
  gap:8px;
  align-items:center;
  flex-wrap:wrap;
  box-shadow:var(--shadow);
}
#search-input {
  flex:1;
  min-width:180px;
  background:var(--bg3);
  border:1px solid var(--border);
  border-radius:6px;
  color:var(--text);
  padding:7px 11px;
  font-size:13px;
  outline:none;
}
#search-input:focus { border-color:var(--accent); }
.btn-group { display:flex; gap:3px; }
.tbtn {
  padding:5px 11px;
  border:1px solid var(--border);
  border-radius:6px;
  background:var(--bg3);
  color:var(--text2);
  cursor:pointer;
  font-size:12px;
  white-space:nowrap;
  transition:all 0.12s;
}
.tbtn:hover { border-color:var(--accent); color:var(--text); }
.tbtn.active { background:var(--accent2); border-color:var(--accent); color:var(--accent-fg); }
#btn-search { background:var(--accent2); border-color:var(--accent); color:var(--accent-fg); }
#btn-search:hover { filter:brightness(1.1); }
.tbtn.pending { box-shadow:0 0 0 2px var(--accent); }
#search-stats { font-size:11px; color:var(--text3); white-space:nowrap; min-width:80px; }
#nav-btns { display:none; gap:3px; }

/* live-search toggle */
.tbtn-check {
  display:inline-flex;
  align-items:center;
  gap:4px;
  cursor:pointer;
  user-select:none;
}
.tbtn-check input {
  margin:0;
  accent-color:var(--accent);
  cursor:pointer;
}

/* theme toggle */
#theme-toggle { font-size:16px; cursor:pointer; background:none; border:none; color:var(--text2); }
#theme-toggle:hover { color:var(--text); }

/* ── Scope buttons ── */
.scope-lbl { font-size:11px; color:var(--text3); }

/* ── Content ── */
#content { padding:20px 24px; max-width:900px; margin:0 auto; width:100%; }

/* ── Chat section ── */
.chat-section {
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:10px;
  margin-bottom:24px;
  overflow:hidden;
  box-shadow:var(--shadow);
}
.chat-section.hidden { display:none; }
.chat-header {
  padding:14px 18px;
  border-bottom:1px solid var(--border);
  background:var(--bg3);
}
.chat-title-row { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
.chat-title { font-size:15px; font-weight:700; }
.chat-badge {
  font-size:10px; padding:2px 6px; border-radius:4px;
  background:var(--bg4); color:var(--accent);
  border:1px solid var(--border);
  text-transform:uppercase; letter-spacing:0.05em; font-weight:600;
}
.chat-meta { display:flex; flex-wrap:wrap; gap:10px; font-size:12px; color:var(--text2); }
.chat-src { color:var(--text3); font-family:monospace; font-size:11px; }
.messages-container { padding:10px; display:flex; flex-direction:column; gap:6px; }

/* ── Messages ── */
.message {
  border-radius:8px;
  padding:10px 13px;
  position:relative;
}
.msg-user      { background:var(--user-bg);   border:1px solid var(--user-border);   border-left:3px solid var(--accent); }
.msg-assistant { background:var(--ai-bg);     border:1px solid var(--ai-border);     border-left:3px solid #4ec94e; }
.msg-system    { background:var(--system-bg); border:1px solid var(--system-border); border-left:3px solid #e8c84e; opacity:.8; }
.msg-tool      { background:var(--tool-bg);   border:1px solid var(--tool-border);   border-left:3px solid #e87c4e; font-family:monospace; font-size:12px; }
.msg-header { display:flex; align-items:center; gap:7px; margin-bottom:5px; }
.msg-role { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--text2); }
.msg-user .msg-role      { color:var(--accent); }
.msg-assistant .msg-role { color:#4ec94e; }
.msg-system .msg-role    { color:#e8c84e; }
.msg-tool .msg-role      { color:#e87c4e; }
.msg-time { font-size:10px; color:var(--text3); font-family:monospace; }
.msg-model-tag { font-size:10px; color:var(--text3); background:var(--bg4); padding:1px 4px; border-radius:3px; font-family:monospace; }
.msg-body { font-size:13px; word-break:break-word; overflow-wrap:break-word; }
.msg-attachments { margin-top:7px; display:flex; flex-wrap:wrap; gap:5px; }
.attach-badge { font-size:11px; background:var(--bg4); border:1px solid var(--border); border-radius:4px; padding:2px 7px; color:var(--text2); }

/* ── Thinking blocks ── */
.think-block {
  margin:6px 0;
  background:var(--think-bg);
  border:1px solid var(--think-border);
  border-radius:6px;
  padding:4px 10px;
}
.think-block summary {
  cursor:pointer;
  font-size:11px;
  color:var(--text3);
  user-select:none;
}
.think-content { margin-top:6px; font-size:12px; color:var(--text2); }

/* ── Code ── */
pre {
  background:var(--code-bg);
  border:1px solid var(--border);
  border-radius:6px;
  padding:10px;
  overflow-x:auto;
  margin:6px 0;
  font-family:"JetBrains Mono","Fira Mono",monospace;
  font-size:12px;
  line-height:1.5;
}
code { font-family:"JetBrains Mono","Fira Mono",monospace; font-size:12px; background:var(--code-bg); padding:1px 4px; border-radius:3px; }
pre code { background:none; padding:0; }

/* ── Tooltip ── */
.has-tooltip { cursor:help; }
.msg-tooltip {
  display:none;
  position:absolute;
  right:8px; top:8px;
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:6px;
  box-shadow:0 4px 16px rgba(0,0,0,0.18);
  z-index:200;
  min-width:260px;
  max-width:400px;
  font-size:11px;
  padding:6px;
}
.has-tooltip:hover .msg-tooltip { display:block; }
.msg-tooltip table { border-collapse:collapse; width:100%; }
.tt-key { color:var(--accent); padding:2px 6px 2px 2px; white-space:nowrap; font-weight:600; }
.tt-val { color:var(--text2); word-break:break-all; padding:2px; }

/* ── Search highlights ── */
mark.hl { background:var(--hl-bg); color:var(--hl-color); border-radius:2px; padding:0 1px; }
mark.hl.current { background:var(--hl-cur-bg); color:var(--text); }
.message.has-match { box-shadow:0 0 0 1px var(--accent); }
.message.match-hidden { opacity:.2; pointer-events:none; }

/* ── Model Filter ── */
#model-filter {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 20px;
  box-shadow: var(--shadow);
}
#model-filter summary {
  font-weight: 600;
  cursor: pointer;
  user-select: none;
  color: var(--accent);
}
#model-checkboxes {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.filter-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
  user-select: none;
  padding: 2px 6px;
  border-radius: 4px;
}
.filter-item:hover {
  background: var(--bg3);
}
.filter-group {
  margin-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-left: 1px solid var(--border);
  padding-left: 8px;
  margin-top: 2px;
  margin-bottom: 4px;
}

/* ── Empty state & hidden models ── */
.message.model-hidden { display:none !important; }
.chat-section.model-hidden-chat { display:none !important; }
.sidebar-item.model-hidden-chat { display:none !important; }
#no-results { display:none; text-align:center; padding:60px 20px; color:var(--text3); }
#no-results.visible { display:block; }

/* ── Footer ── */
#footer { text-align:center; padding:16px; color:var(--text3); font-size:11px; border-top:1px solid var(--border); margin-top:8px; }

::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
</style>
</head>
<body>

<div id="sidebar">
  <div id="sidebar-header">
    <h1>⚡ Chat Archive</h1>
    <div id="sidebar-stats">TOTAL_CHATS chats &middot; TOTAL_MSGS messages</div>
  </div>
  <div id="sidebar-search">
    <input id="sidebar-filter" type="text" placeholder="Filter chats…" oninput="filterSidebar(this.value)">
  </div>
  <div id="sidebar-list">
SIDEBAR_ITEMS
  </div>
</div>

<div id="main">
  <div id="toolbar">
    <input id="search-input" type="text" placeholder="Search across all chats…">
    <button class="tbtn" id="btn-search" onclick="runSearchNow()" title="Run search now">&#128269; Search</button>
    <div class="btn-group" id="mode-btns">
      <button class="tbtn active" id="btn-plain" onclick="setMode('plain')">Plain</button>
      <button class="tbtn" id="btn-regex" onclick="setMode('regex')">Regex</button>
      <button class="tbtn" id="btn-fuzzy" onclick="setMode('fuzzy')">Fuzzy</button>
    </div>
    <span class="scope-lbl">in:</span>
    <div class="btn-group" id="scope-btns">
      <button class="tbtn active" id="scope-all"   onclick="setScope('all')">All</button>
      <button class="tbtn"        id="scope-user"  onclick="setScope('user')">User</button>
      <button class="tbtn"        id="scope-ai"    onclick="setScope('ai')">AI</button>
      <button class="tbtn"        id="scope-title" onclick="setScope('title')">Titles</button>
    </div>
    <div class="btn-group" id="nav-btns">
      <button class="tbtn" onclick="navMatch(-1)">&#9650;</button>
      <button class="tbtn" onclick="navMatch(1)">&#9660;</button>
    </div>
    <span id="search-stats"></span>
    <button class="tbtn" onclick="clearSearch()">&#10005;</button>
    <label class="tbtn tbtn-check" title="Search as you type"><input type="checkbox" id="live-search" onchange="toggleLive()"> Live</label>
    <button id="theme-toggle" onclick="toggleTheme()" title="Toggle theme">🌙</button>
  </div>

  <div id="content">
    <details id="model-filter">
      <summary>⚙️ Filter by Model</summary>
      <div id="model-filter-actions" style="margin-top: 10px; display: flex; gap: 6px;">
        <button class="tbtn" onclick="toggleAllModels(true)">Check All</button>
        <button class="tbtn" onclick="toggleAllModels(false)">Uncheck All</button>
      </div>
      <div id="model-checkboxes"></div>
    </details>
CHATS_HTML
    <div id="no-results">No chats match your search.</div>
  </div>
  <div id="footer">Generated by ai_chat_archive.py &middot; GEN_TIME &middot; SOURCE_ROOT</div>
</div>

<script src="SEARCH_INDEX_FILE"></script>
<script>
(function() {
'use strict';

// ── Data ──
// SEARCH_INDEX is set by search_index.js

// ── State ──
var mode = 'plain';
var scope = 'all';
var allMatches = [];
var currentMatch = -1;
var liveSearch = false;  // "Search as you type" toggle
var lastSearched = '';   // query text of the last executed search
var activeModels = {};

// ── Theme ──
function toggleTheme() {
  var html = document.documentElement;
  var next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  document.getElementById('theme-toggle').textContent = next === 'dark' ? '☀️' : '🌙';
  try { localStorage.setItem('cwTheme', next); } catch(e) {}
}
// Restore saved theme
(function() {
  try {
    var t = localStorage.getItem('cwTheme');
    if (t) {
      document.documentElement.setAttribute('data-theme', t);
      var btn = document.getElementById('theme-toggle');
      if (btn) btn.textContent = t === 'dark' ? '☀️' : '🌙';
    }
  } catch(e) {}
})();

// ── Debounced, stale-safe search scheduling ──
var SEARCH_DELAY = 200;
var searchTimer = null;
var searchToken = 0;

function scheduleSearch() {
  clearTimeout(searchTimer);
  var token = ++searchToken;
  searchTimer = setTimeout(function() { runSearch(token); }, SEARCH_DELAY);
}

function runSearch(token) {
  if (token !== searchToken) return; // stale — a newer search is pending
  doSearch(document.getElementById('search-input').value, false);
}

// ── Explicit search (Search button / Enter) ──
function runSearchNow() {
  clearTimeout(searchTimer);
  var token = ++searchToken;
  doSearch(document.getElementById('search-input').value, true);
}

// ── Live-search toggle ──
function toggleLive() {
  var el = document.getElementById('live-search');
  liveSearch = !!(el && el.checked);
  try { localStorage.setItem('cwLive', liveSearch ? '1' : '0'); } catch(e) {}
  if (liveSearch) {
    if (document.getElementById('search-input').value) scheduleSearch();
  } else {
    searchToken++; clearTimeout(searchTimer); // cancel any pending live search
  }
  updatePending();
}
// Restore saved live setting
(function() {
  try {
    var v = localStorage.getItem('cwLive');
    if (v !== null) {
      liveSearch = v === '1';
      var el = document.getElementById('live-search');
      if (el) el.checked = liveSearch;
    }
  } catch(e) {}
})();

// ── "Search" button pending hint (typed != last executed) ──
function updatePending() {
  var btn = document.getElementById('btn-search');
  if (!btn) return;
  var typed = document.getElementById('search-input').value.trim();
  btn.classList.toggle('pending', typed.length > 0 && typed !== lastSearched);
}

// ── Mode ──
function setMode(m) {
  mode = m;
  ['plain','regex','fuzzy'].forEach(function(x) {
    document.getElementById('btn-' + x).classList.toggle('active', x === m);
  });
  scheduleSearch();
}

// ── Scope ──
function setScope(s) {
  scope = s;
  ['all','user','ai','title'].forEach(function(x) {
    document.getElementById('scope-' + x).classList.toggle('active', x === s);
  });
  scheduleSearch();
}

// ── Sidebar filter (debounced) ──
var SIDEBAR_DELAY = 150;
var sidebarTimer = null;
function filterSidebar(q) {
  clearTimeout(sidebarTimer);
  sidebarTimer = setTimeout(function() {
    q = (q || '').toLowerCase();
    document.querySelectorAll('.sidebar-item').forEach(function(el) {
      el.classList.toggle('hidden', q.length > 0 && el.textContent.toLowerCase().indexOf(q) === -1);
    });
  }, SIDEBAR_DELAY);
}

// ── Jump to chat ──
function jumpToChat(idx) {
  document.querySelectorAll('.sidebar-item').forEach(function(el) { el.classList.remove('active'); });
  var si = document.querySelector('.sidebar-item[data-idx="' + idx + '"]');
  if (si) si.classList.add('active');
  var sec = document.getElementById('chat-' + idx);
  if (sec) sec.scrollIntoView({behavior:'smooth', block:'start'});
}

// ── Search limits (big-DB safety) ──
var MAX_HL_MSGS = 400;    // max messages/titles visually marked per search
var MAX_HL_MARKS = 1500;  // hard cap on total <mark> nodes rendered
var FUZZY_WINDOW_MAX = 2000; // skip windowed Levenshtein on longer texts

// ── Per-query working state (set at the start of doSearch) ──
var currentQuery = '';
var currentQL = '';
var currentP = '';
var currentREG = null;

// ── Fast search index (built once from the embedded SEARCH_INDEX) ──
var MESSAGES = []; // {c: chatIdx, m: msgIdx, r: role, t: lowercased content}
var CHATS = [];    // {i: chatIdx, t: lowercased "title model", title: lowercased title}

// Build search index synchronously (SEARCH_INDEX loaded via script tag)
(function() {
  if (!SEARCH_INDEX) return;
  var uniqueModels = {};
  for (var k = 0; k < SEARCH_INDEX.length; k++) {
    var e = SEARCH_INDEX[k];
    if (e.model) uniqueModels[e.model] = true;
    CHATS.push({i: e.i,
                t: (String(e.title || '') + ' ' + String(e.model || '')).toLowerCase(),
                title: String(e.title || '').toLowerCase()});
    var ms = e.msgs;
    if (!ms || !ms.length) {
      if (e.text) MESSAGES.push({c: k, m: 0, r: 'assistant', t: String(e.text).toLowerCase(), mod: String(e.model || '')});
      continue;
    }
    for (var j = 0; j < ms.length; j++) {
      var txt = String(ms[j].t || '');
      var mModel = String(ms[j].m || e.model || '');
      if (mModel) uniqueModels[mModel] = true;
      if (txt) MESSAGES.push({c: k, m: j, r: ms[j].r || 'assistant', t: txt, mod: mModel});
    }
  }
  
  var mKeys = Object.keys(uniqueModels).sort();
  var container = document.getElementById('model-checkboxes');

  function getModelPath(m) {
    var low = m.toLowerCase();
    if (low.startsWith('gpt')) {
      if (low.startsWith('gpt-4')) return ['GPT', 'GPT-4', m];
      if (low.startsWith('gpt-5')) return ['GPT', 'GPT-5', m];
      return ['GPT', 'Other GPT', m];
    }
    if (low.startsWith('grok')) {
      if (low.startsWith('grok-3')) return ['Grok', 'Grok-3', m];
      if (low.startsWith('grok-4')) return ['Grok', 'Grok-4', m];
      return ['Grok', m];
    }
    if (low.startsWith('qwen')) return ['Qwen', m];
    if (low.startsWith('deepseek')) return ['DeepSeek', m];
    if (low.match(/^o\d/)) return ['O-Series (o3/o4)', m];
    if (low.includes('davinci')) return ['Legacy (Davinci)', m];
    if (low.includes('gemini') || low === 'режим ии') return ['Gemini', m];
    return ['Other', m];
  }

  if (mKeys.length > 0 && container) {
    var tree = { _nodes: {} };
    mKeys.forEach(function(m) {
      activeModels[m] = true;
      var path = getModelPath(m);
      var curr = tree;
      for (var i = 0; i < path.length; i++) {
        var p = path[i];
        if (!curr._nodes[p]) curr._nodes[p] = { _nodes: {}, _isLeaf: false, name: p };
        curr = curr._nodes[p];
      }
      curr._isLeaf = true;
      curr.modelName = m;
    });

    function renderTree(nodesObj, parentEl) {
      var keys = Object.keys(nodesObj).sort();
      keys.forEach(function(k) {
        var node = nodesObj[k];
        var itemDiv = document.createElement('div');
        var lbl = document.createElement('label');
        lbl.className = 'filter-item';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = true;
        
        if (node._isLeaf) {
          cb.value = node.modelName;
          cb.className = 'cb-model';
          cb.onchange = function() {
            activeModels[this.value] = this.checked;
            updateModelVisibility();
          };
          lbl.appendChild(cb);
          lbl.appendChild(document.createTextNode(node.modelName));
          itemDiv.appendChild(lbl);
        } else {
          cb.className = 'cb-group';
          cb.onchange = function() {
            var checked = this.checked;
            var groupDiv = this.parentNode.nextElementSibling;
            if (groupDiv) {
              groupDiv.querySelectorAll('.cb-model').forEach(function(childCb) {
                childCb.checked = checked;
                activeModels[childCb.value] = checked;
              });
              groupDiv.querySelectorAll('.cb-group').forEach(function(gCb) {
                gCb.checked = checked;
              });
            }
            updateModelVisibility();
          };
          lbl.appendChild(cb);
          lbl.appendChild(document.createTextNode(node.name));
          itemDiv.appendChild(lbl);
          
          var groupDiv = document.createElement('div');
          groupDiv.className = 'filter-group';
          renderTree(node._nodes, groupDiv);
          itemDiv.appendChild(groupDiv);
        }
        parentEl.appendChild(itemDiv);
      });
    }

    renderTree(tree._nodes, container);
  } else {
    var filterEl = document.getElementById('model-filter');
    if (filterEl) filterEl.style.display = 'none';
  }
})();

function updateModelVisibility() {
  document.querySelectorAll('.message').forEach(function(el) {
    var m = el.getAttribute('data-model');
    var isVisible = !m || activeModels[m];
    el.classList.toggle('model-hidden', !isVisible);
  });
  
  document.querySelectorAll('.chat-section').forEach(function(sec) {
     var msgs = sec.querySelectorAll('.message');
     var anyVisible = false;
     for (var i = 0; i < msgs.length; i++) {
        if (!msgs[i].classList.contains('model-hidden')) { anyVisible = true; break; }
     }
     sec.classList.toggle('model-hidden-chat', !anyVisible);
  });
  
  if (document.getElementById('search-input').value.trim()) {
    scheduleSearch();
  } else {
    var items = document.querySelectorAll('.sidebar-item');
    for (var i = 0; i < items.length; i++) {
      var si = items[i];
      var secEl = document.getElementById('chat-' + si.dataset.idx);
      var isHiddenByModel = secEl && secEl.classList.contains('model-hidden-chat');
      si.classList.toggle('model-hidden-chat', !!isHiddenByModel);
    }
  }
}

// ── Fuzzy scoring (bigram + bounded windowed Levenshtein) ──
// t must be pre-lowercased, p is lowercased.
function fuzzyMatchScore(t, p) {
  if (!p) return 0;
  if (t.indexOf(p) !== -1) return 1;
  if (p.length < 2) return 0;
  var hits = 0, n = p.length - 1;
  for (var i = 0; i < n; i++) {
    if (t.indexOf(p.charAt(i) + p.charAt(i + 1)) !== -1) hits++;
  }
  var score = hits / n;
  // Windowed Levenshtein only helps borderline cases: skip it when the bigram
  // score already crossed the threshold or nothing overlaps (result is fixed).
  if (p.length <= 10 && t.length <= FUZZY_WINDOW_MAX && hits > 0 && score < 0.65) {
    var best = p.length, end = t.length - p.length;
    for (var j = 0; j <= end; j++) {
      var d = 0;
      for (var k = 0; k < p.length; k++) if (t[j + k] !== p[k]) d++;
      if (d < best) best = d;
      if (best === 0) break;
    }
    var ws = 1 - best / p.length;
    if (ws > score) score = ws;
  }
  return score;
}

// Boolean "is this (pre-lowercased) text a fuzzy match?"
function fuzzyDecide(text) {
  if (!text || !currentP) return false;
  return fuzzyMatchScore(text, currentP) >= 0.65;
}

// ── Count occurrences in one text (no range allocations) ──
function countMatches(text) {
  if (!text) return 0;
  if (mode === 'plain' && !currentQL) return 0;
  var n = 0;
  if (mode === 'plain') {
    var pos = 0, idx;
    while ((idx = text.indexOf(currentQL, pos)) !== -1) { n++; pos = idx + 1; }
  } else if (mode === 'regex') {
    if (!currentREG) return 0;
    var m;
    currentREG.lastIndex = 0;
    while ((m = currentREG.exec(text)) !== null) {
      n++;
      if (m[0].length === 0) currentREG.lastIndex++;
    }
  } else {
    return fuzzyDecide(text) ? 1 : 0;
  }
  return n;
}

// ── Find ranges in text (for DOM highlighting) ──
function findRanges(text) {
  var results = [];
  if (!currentQuery || !text) return results;
  var pos, idx, m;
  if (mode === 'plain') {
    var tl = text.toLowerCase();
    pos = 0;
    while ((idx = tl.indexOf(currentQL, pos)) !== -1) {
      results.push([idx, idx + currentQL.length]);
      pos = idx + 1;
    }
  } else if (mode === 'regex') {
    if (currentREG) {
      try {
        currentREG.lastIndex = 0;
        while ((m = currentREG.exec(text)) !== null) {
          results.push([m.index, m.index + m[0].length]);
          if (m[0].length === 0) currentREG.lastIndex++;
        }
      } catch (e) {}
    }
  } else {
    results = fuzzyRanges(text);
  }
  return results;
}

function fuzzyRanges(text) {
  var results = [];
  var t = text.toLowerCase(), p = currentP;
  if (t.length > FUZZY_WINDOW_MAX) {
    if (fuzzyMatchScore(t, p) >= 0.65) results.push([0, Math.min(p.length, t.length)]);
    return results;
  }
  var wlen = Math.max(p.length, 3), i = 0;
  while (i <= t.length - wlen) {
    if (fuzzyMatchScore(t.slice(i, i + wlen), p) >= 0.65) {
      results.push([i, i + wlen]);
      i += wlen;
    } else { i++; }
  }
  return results;
}

// ── Highlight text nodes; returns the number of marks created ──
function highlightEl(el, ranges, maxMarks) {
  if (!ranges || !ranges.length) return 0;
  var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  var nodes = [], node;
  while ((node = walker.nextNode())) nodes.push(node);
  var offset = 0;
  var nodeMap = nodes.map(function(n) {
    var s = offset; offset += n.textContent.length;
    return {node:n, start:s, end:offset};
  });
  var sorted = ranges.slice().sort(function(a,b) { return b[0]-a[0]; });
  var created = 0;
  for (var ri = 0; ri < sorted.length; ri++) {
    if (created >= maxMarks) break;
    var range = sorted[ri];
    var rs = range[0], re = range[1];
    for (var ni = 0; ni < nodeMap.length; ni++) {
      if (created >= maxMarks) break;
      var no = nodeMap[ni];
      if (re <= no.start || rs >= no.end) continue;
      var ls = Math.max(rs, no.start) - no.start;
      var le = Math.min(re, no.end) - no.start;
      var t = no.node.textContent;
      var mark = document.createElement('mark');
      mark.className = 'hl';
      mark.textContent = t.slice(ls, le);
      var after = no.node.splitText(ls);
      after.textContent = t.slice(le);
      no.node.parentNode.insertBefore(mark, after);
      allMatches.push(mark);
      created++;
    }
  }
  return created;
}

// ── Elements touched by the current search (for cheap cleanup) ──
var touchedSections = [];
var touchedMsgs = [];
var hlContents = [];
var hlDone = 0, markTotal = 0;

function hlMessage(msgEl) {
  if (hlDone >= MAX_HL_MSGS || markTotal >= MAX_HL_MARKS) return;
  var contentEl = msgEl.querySelector('.search-content');
  if (!contentEl) return;
  var ranges = findRanges(contentEl.textContent);
  if (!ranges.length) return;
  hlContents.push(contentEl);
  markTotal += highlightEl(contentEl, ranges, MAX_HL_MARKS - markTotal);
  hlDone++;
}

// ── Clear highlights (only elements the previous search touched) ──
function clearHighlights() {
  var i, el;
  for (i = 0; i < hlContents.length; i++) {
    el = hlContents[i];
    el.querySelectorAll('mark.hl').forEach(function(m) {
      m.parentNode.replaceChild(document.createTextNode(m.textContent), m);
    });
    el.normalize();
  }
  hlContents = [];
  for (i = 0; i < touchedMsgs.length; i++) {
    touchedMsgs[i].classList.remove('has-match','match-hidden');
  }
  touchedMsgs = [];
  for (i = 0; i < touchedSections.length; i++) {
    touchedSections[i].classList.remove('hidden');
  }
  touchedSections = [];
  document.getElementById('no-results').classList.remove('visible');
  document.getElementById('nav-btns').style.display = 'none';
  document.getElementById('search-stats').textContent = '';
  allMatches = []; currentMatch = -1;
}

// ── Main search ──
function doSearch(query, explicit) {
  clearHighlights();
  hlDone = 0; markTotal = 0;
  if (!query || !query.trim()) { lastSearched = ''; updatePending(); return; }
  var q = query.trim();
  currentQuery = q;
  currentQL = q.toLowerCase();
  currentP = q.toLowerCase();
  currentREG = null;
  if (mode === 'regex') {
    try { currentREG = new RegExp(q, 'gi'); } catch (e) { currentREG = null; }
  }

  var totalHits = 0, visibleChats = 0, scopeRole = null;
  if (scope === 'user') scopeRole = 'user';
  else if (scope === 'ai') scopeRole = 'assistant';

  var msgMatch = {}, dimByRole = {}, chatHits = {};
  var i, n, rec, c;

  if (scope === 'title') {
    for (i = 0; i < CHATS.length; i++) {
      c = countMatches(CHATS[i].title);
      if (c > 0) { totalHits += c; chatHits[CHATS[i].i] = c; }
    }
  } else {
    for (n = 0; n < MESSAGES.length; n++) {
      rec = MESSAGES[n];
      if (rec.mod && !activeModels[rec.mod]) {
        continue;
      }
      if (scopeRole && rec.r !== scopeRole) {
        if (!dimByRole[rec.c]) dimByRole[rec.c] = {};
        dimByRole[rec.c][rec.m] = true;
        continue;
      }
      c = countMatches(rec.t);
      if (c > 0) {
        totalHits += c;
        chatHits[rec.c] = (chatHits[rec.c] || 0) + c;
        if (!msgMatch[rec.c]) msgMatch[rec.c] = {};
        msgMatch[rec.c][rec.m] = true;
      }
    }
  }

  var sections = document.querySelectorAll('.chat-section');
  for (i = 0; i < sections.length; i++) {
    var sec = sections[i];
    var idx = sec.getAttribute('data-idx');
    touchedSections.push(sec);
    if (!chatHits[idx]) { sec.classList.add('hidden'); continue; }
    sec.classList.remove('hidden');
    visibleChats++;

    if (scope === 'title') {
      if (hlDone < MAX_HL_MSGS && markTotal < MAX_HL_MARKS) {
        var titleEl = sec.querySelector('.chat-title');
        if (titleEl) {
          var tRanges = findRanges(titleEl.textContent);
          if (tRanges.length) {
            hlContents.push(titleEl);
            markTotal += highlightEl(titleEl, tRanges, MAX_HL_MARKS - markTotal);
            hlDone++;
          }
        }
      }
      continue;
    }

    var msgs = sec.querySelectorAll('.message');
    for (n = 0; n < msgs.length; n++) {
      var mEl = msgs[n];
      var midx = mEl.getAttribute('data-idx');
      var isMatch = !!(msgMatch[idx] && msgMatch[idx][midx]);
      touchedMsgs.push(mEl);
      if (isMatch) {
        mEl.classList.add('has-match');
        mEl.classList.remove('match-hidden');
        hlMessage(mEl);
      } else {
        mEl.classList.add('match-hidden');
      }
    }
  }

  // Sync sidebar
  var items = document.querySelectorAll('.sidebar-item');
  for (i = 0; i < items.length; i++) {
    var si = items[i];
    var secEl = document.getElementById('chat-' + si.dataset.idx);
    si.classList.toggle('hidden', !!(secEl && secEl.classList.contains('hidden')));
  }

  if (visibleChats === 0) document.getElementById('no-results').classList.add('visible');

  lastSearched = q;
  updatePending();

  if (totalHits > 0) {
    document.getElementById('nav-btns').style.display = 'flex';
    document.getElementById('search-stats').textContent = totalHits + ' match' + (totalHits !== 1 ? 'es' : '') + ' in ' + visibleChats + ' chat' + (visibleChats !== 1 ? 's' : '');
    if (explicit) navMatch(1, false); // auto-scroll only on explicit searches
  } else {
    document.getElementById('search-stats').textContent = 'No matches';
  }
}

// ── Navigate matches ──
function navMatch(dir, smooth) {
  if (!allMatches.length) return;
  if (currentMatch >= 0 && currentMatch < allMatches.length)
    allMatches[currentMatch].classList.remove('current');
  currentMatch = (currentMatch + dir + allMatches.length) % allMatches.length;
  allMatches[currentMatch].classList.add('current');
  allMatches[currentMatch].scrollIntoView({behavior: smooth ? 'smooth' : 'auto', block:'center'});
  document.getElementById('search-stats').textContent = (currentMatch+1) + ' / ' + allMatches.length + ' matches';
}

function clearSearch() {
  document.getElementById('search-input').value = '';
  lastSearched = '';
  searchToken++; clearTimeout(searchTimer);
  clearHighlights();
  updatePending();
}

// ── Wire up input ──
document.getElementById('search-input').addEventListener('input', function() {
  updatePending();
  if (liveSearch) scheduleSearch();
});
document.getElementById('search-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') { runSearchNow(); e.preventDefault(); }
  if (e.key === 'Escape') clearSearch();
});

// ── Sidebar active on scroll ──
var observer = new IntersectionObserver(function(entries) {
  entries.forEach(function(entry) {
    if (entry.isIntersecting) {
      var idx = entry.target.dataset.idx;
      document.querySelectorAll('.sidebar-item').forEach(function(el) { el.classList.remove('active'); });
      var si = document.querySelector('.sidebar-item[data-idx="' + idx + '"]');
      if (si) { si.classList.add('active'); si.scrollIntoView({behavior:'smooth', block:'nearest'}); }
    }
  });
}, {threshold:0.1});
document.querySelectorAll('.chat-section').forEach(function(el) { observer.observe(el); });

// Expose globals
window.setMode = setMode;
window.setScope = setScope;
window.filterSidebar = filterSidebar;
window.jumpToChat = jumpToChat;
window.navMatch = navMatch;
window.clearSearch = clearSearch;
window.toggleTheme = toggleTheme;
window.runSearchNow = runSearchNow;
window.toggleLive = toggleLive;
window.toggleAllModels = function(state) {
  var container = document.getElementById('model-checkboxes');
  if (!container) return;
  container.querySelectorAll('input[type="checkbox"]').forEach(function(cb) {
    cb.checked = state;
  });
  Object.keys(activeModels).forEach(function(k) {
    activeModels[k] = state;
  });
  updateModelVisibility();
};

})();
</script>
</body>
</html>""".replace(
        "SIDEBAR_ITEMS", sidebar_html
    ).replace(
        "CHATS_HTML", chats_html
    ).replace(
        "SEARCH_INDEX_FILE", os.path.basename(output_filename).replace(".html", "_search_index.js")
    ).replace(
        "TOTAL_CHATS", str(len(chats))
    ).replace(
        "TOTAL_MSGS", str(total_msgs)
    ).replace(
        "GEN_TIME", esc(gen_time)
    ).replace(
        "SOURCE_ROOT", esc(source_root)
    )

    # Return both HTML and search index JS string
    return html, search_js


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="ai_chat_archive — convert chat JSON exports to a single searchable HTML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CLI flags override CONFIG defaults at the top of the script.

Examples:
  python ai_chat_archive.py                           # uses DEFAULT_DIRECTORY
  python ai_chat_archive.py ./exports                 # scan specific directory
  python ai_chat_archive.py -o archive.html           # custom output filename
  python ai_chat_archive.py -v --tooltips             # verbose + JSON field tooltips
  python ai_chat_archive.py --on-unknown ignore       # skip unrecognized files
""")
    parser.add_argument("directory", nargs="?", default=None,
        help=f"Directory to scan (overrides DEFAULT_DIRECTORY={DEFAULT_DIRECTORY!r})")
    parser.add_argument("-o","--output", default=None,
        help=f"Output HTML file (default: {DEFAULT_OUTPUT!r})")
    parser.add_argument("-v","--verbose", action="store_true", default=None,
        help="Show per-file format detection details")
    parser.add_argument("--tooltips", action="store_true", default=None,
        help="Show JSON field tooltips on message hover")
    parser.add_argument("--on-unknown", choices=["ignore","try"], default=None,
        help="'ignore' skips unrecognized files; 'try' attempts generic parse")
    args = parser.parse_args()

    # CLI args override CONFIG defaults
    directory  = args.directory  if args.directory  is not None else DEFAULT_DIRECTORY
    output     = args.output     if args.output     is not None else DEFAULT_OUTPUT
    verbose    = args.verbose    if args.verbose     is not None else DEFAULT_VERBOSE
    tooltips   = args.tooltips   if args.tooltips    is not None else DEFAULT_TOOLTIPS
    on_unknown = args.on_unknown if args.on_unknown  is not None else DEFAULT_ON_UNKNOWN

    root = os.path.abspath(directory) if directory else os.getcwd()
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"📂 Scanning: {root}")
    json_files = collect_json_files(root)
    print(f"   Found {len(json_files)} JSON file(s)")

    all_chats: List[Chat] = []
    for path in json_files:
        rel = os.path.relpath(path, root)
        try:
            data, was_repaired = load_json_lenient(path)
        except Exception as e:
            if verbose:
                print(f"  [SKIP] {rel}: JSON parse error (unrepairable) — {e}", file=sys.stderr)
            continue

        detected = FormatRegistry.detect(data)
        fmt_name = detected.name if detected else "unknown"
        chats = FormatRegistry.parse(data, path, on_unknown=on_unknown)
        repaired_tag = " [repaired]" if was_repaired else ""

        if verbose:
            status = "✓" if chats else ("·" if not detected else "⚠")
            print(f"  {status} {rel}  [{fmt_name}]{repaired_tag}  → {len(chats)} chat(s), "
                  f"{sum(len(c.messages) for c in chats)} msg(s)")
        elif chats:
            print(f"  ✓ {rel} [{fmt_name}]{repaired_tag} → {len(chats)} chat(s), "
                  f"{sum(len(c.messages) for c in chats)} msg(s)")
        elif detected and on_unknown == "try":
            print(f"  ⚠ {rel} [{fmt_name}]{repaired_tag} → no messages extracted", file=sys.stderr)

        all_chats.extend(chats)

    if not all_chats:
        print("\n⚠  No chats extracted.")
        print("   Run with -v to see per-file detection.")
        sys.exit(0)

    total_msgs = sum(len(c.messages) for c in all_chats)
    print(f"\n✅ {len(all_chats)} chat(s), {total_msgs} message(s) total")
    print("⚙  Generating HTML…")

    html_out, search_js = render_html(all_chats, root, show_tooltip=tooltips, output_filename=os.path.basename(output))
    
    # Write HTML file
    with open(output, "w", encoding="utf-8") as f:
        f.write(html_out)
        f.flush()
        os.fsync(f.fileno())

    # Write search_index.js file
    search_index_path = os.path.splitext(output)[0] + "_search_index.js"
    with open(search_index_path, "w", encoding="utf-8") as f:
        f.write(search_js)
        f.flush()
        os.fsync(f.fileno())

    size_kb = os.path.getsize(output) // 1024
    # sanity check: file ends with </html> (not truncated mid-script)
    with open(output, "rb") as f:
        f.seek(max(0, os.path.getsize(output) - 256))
        tail = f.read().decode("utf-8", errors="ignore")
        if "</html>" not in tail:
            print(f"⚠ WARNING: output may be truncated (no </html> in last 256 bytes)", file=sys.stderr)
    print(f"💾 Saved: {output}  ({size_kb} KB)")
    print(f"💾 Saved: {search_index_path}")
    print("   Open in any browser — no server needed.")


if __name__ == "__main__":
    main()
