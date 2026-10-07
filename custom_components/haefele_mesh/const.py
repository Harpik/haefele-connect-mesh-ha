"""Constants for Häfele Connect Mesh integration."""

DOMAIN = "haefele_mesh"

CONF_NETWORK_KEY = "network_key"
CONF_APP_KEY = "app_key"
CONF_IV_INDEX = "iv_index"
CONF_NODES = "nodes"
# Marks entries created with per-entry SEQ storage (see coordinator).
# Absent on entries created by earlier releases.
CONF_SEQ_STORE = "seq_store"
SEQ_STORE_PER_ENTRY = "per_entry"

# BLE
MESH_PROXY_SERVICE_UUID  = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_IN_UUID  = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"

# Heartbeat
HEARTBEAT_INTERVAL = 60  # seconds

# Mesh crypto
IV_INDEX_DEFAULT = 1
# Base address used for our BT Mesh SRC.
#
# Must NOT collide with SRCs used by any previous provisioner/gateway for the
# same network, or the lights' anti-replay cache will silently drop our
# frames until our SEQ exceeds whatever they last accepted. The Pi3 gateway
# used 0x0060/0x0080, and the Haefele mobile app uses the provisioner
# address from the .connect file (typically 0x7FFD). 0x00C0+ is fresh in all
# known deployments, leaving 0x10 headroom between nodes.
# Our mesh SRC. 0x00C8 is in an unallocated range for this network,
# picked to avoid collisions with real Häfele nodes (0x0017, 0x002F,
# 0x003A), the provisioner (0x7FFD, used by the Häfele app) and the
# C0xx group addresses we subscribe to.
#
# NOTE: bumped 0x00C0 -> 0x00C8 in 0.4.2. Deleting the persisted SEQ store
# rewound the SEQ counter for the old SRC 0x00C0 *below* the replay
# watermark the lamps had already cached for that address, so every frame
# we emitted looked like a replay and was silently dropped (no Status
# replies, no actuation). Moving to a brand-new SRC the lamps have never
# seen gives us an empty replay list there, so they accept us from the
# very first frame.
SRC_ADDRESS_BASE = 0x00C8

# SRC values shipped by earlier builds. A config entry still carrying one
# of these under "src_address_base" was never a deliberate user override
# (the override read path was broken), so async_migrate_entry rewrites it
# to the current SRC_ADDRESS_BASE.
LEGACY_SRC_ADDRESSES = (0x0060, 0x0080, 0x00C0)

# Lower bound used when seeding a brand-new SEQ counter. Starting at
# 0x800000 (half the 24-bit SEQ space) guarantees we're above anything any
# previous emitter could plausibly have left in the lights' replay cache
# for this SRC, while still leaving ~8M emissions before we need an IV
# Index update — well beyond the lifetime of any install.
SEQ_SEED_MIN = 0x800000

# --- SEQ lifecycle ---------------------------------------------------------
#
# SEQ is a 24-bit counter per SRC. It must never wrap: a frame carrying a
# SEQ at or below what a lamp has already cached for our SRC is treated as
# a replay and dropped silently (the 0.4.2 lockout symptom).
SEQ_MAX = 0xFFFFFF

# Once the active SRC hands out a SEQ at or above this value, the
# coordinator switches to a fresh SRC and starts that one from
# ROTATED_SRC_SEQ_START. The margin below SEQ_MAX (~1M frames, about a
# month of polling on a small network) is only a safety buffer: rotation
# happens on the very first SEQ that crosses the threshold.
SEQ_ROTATE_THRESHOLD = 0xF00000

# A rotated SRC is one we have never emitted from, picked to avoid every
# known node / provisioner / legacy address, so no lamp holds a replay
# entry for it and its SEQ space can start from the bottom.
ROTATED_SRC_SEQ_START = 0

# SEQ persistence is done in blocks: we store a *ceiling* (last handed-out
# SEQ + block) and only write again when the ceiling is reached. After an
# unclean shutdown we resume from the ceiling, which is always >= the
# last SEQ actually used, so the guarantee is unchanged while disk writes
# drop by this factor (matters on SD-card installs).
SEQ_PERSIST_BLOCK = 256

# Upper end of the BT Mesh unicast range.
UNICAST_MAX = 0x7FFF

# Rotation prefers SRCs outside every provisioner's allocated unicast range,
# searching downward from here. 0x7F00-0x7FFF is left alone because that is
# where the Häfele app places provisioner addresses (seen: 0x7FF9, 0x7FFD).
ROTATION_SEARCH_TOP = 0x7EFF

# The config entry only stores each node's primary unicast address, not
# its element count. When picking a rotation SRC we keep this many
# addresses clear starting at every node's primary address.
NODE_ADDRESS_MARGIN = 16

# Light capabilities
LIGHT_MIN_KELVIN = 2700
LIGHT_MAX_KELVIN = 5000
