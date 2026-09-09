"""
Sensor Hub [CLOUD]  --  MQTT subscriber + SQLite writer  (SEN55 + SCD30 + Vision)
- broker    : HiveMQ Cloud (TLS 8883) -- bypasses school-WiFi client isolation
- subscribe : multinode_aq/+/env   (SEN55+SCD30 env node)
              multinode_aq/+/occ            (FOMO vision occupancy node)  << NEW
- store     : SQLite (sensor_data.db)
                readings  table -- env vars (11)
                occupancy table -- occ/occ_med/occ_max/occ_last + cents(JSON) + w + n
- pair      : dashboard.py (read-only)
- policy    : occ* columns = analysis (SQL/join) ; cents = UI-only JSON string
              (raw text stored as-is; dashboard json.loads() when drawing)
"""
import os, json, sqlite3, signal, sys, ssl
import paho.mqtt.client as mqtt

# ---- Config (HiveMQ Cloud) ----
# 접속 정보는 환경변수에서 로드 -- 실제 값은 이 장치의 ~/.config/mintcap/mintcap.env
# (systemd user 서비스의 EnvironmentFile). 템플릿: deploy/mintcap.env.example
BROKER   = os.environ["MQTT_BROKER"]
PORT     = int(os.environ.get("MQTT_PORT", "8883"))
USERNAME = os.environ["MQTT_USERNAME"]
PASSWORD = os.environ["MQTT_PASSWORD"]
TOPIC     = "multinode_aq/+/env"      # env nodes (existing)
TOPIC_OCC = "multinode_aq/+/occ"      # vision nodes (NEW)
DB       = "sensor_data.db"

sys.stdout.reconfigure(line_buffering=True)

# ---- DB init ----
conn = sqlite3.connect(DB, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS readings(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT    DEFAULT CURRENT_TIMESTAMP,   -- UTC; +9h KST in analysis
  node     TEXT,
  pm1p0    REAL,
  pm2p5    REAL,
  pm4p0    REAL,
  pm10p0   REAL,
  sen_temp REAL,
  sen_hum  REAL,
  voc      REAL,
  nox      REAL,
  co2      REAL,
  scd_temp REAL,
  scd_hum  REAL,
  n        INTEGER
)""")
# NEW: vision occupancy (5-min bucket stats; same UTC ts policy as readings ->
#      exact join on (label, ts) with env buckets)
conn.execute("""
CREATE TABLE IF NOT EXISTS occupancy(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT    DEFAULT CURRENT_TIMESTAMP,   -- UTC bucket string from node
  node     TEXT,
  occ      REAL,                                -- bucket mean (headcount)
  occ_med  INTEGER,                             -- bucket median
  occ_max  INTEGER,                             -- bucket max
  occ_last INTEGER,                             -- last sample of the bucket
  cents    TEXT,                                -- centroid JSON "[[x,y],...]" (UI only)
  w        INTEGER,                             -- coordinate frame size (model input)
  n        INTEGER                              -- valid samples in bucket (~30 normal)
)""")
conn.commit()

COLS = ["pm1p0","pm2p5","pm4p0","pm10p0","sen_temp","sen_hum","voc","nox",
        "co2","scd_temp","scd_hum"]

# ---- MQTT callbacks (paho 2.x VERSION2) ----
def on_connect(client, userdata, flags, reason_code, properties):
    print(f"broker connect: {reason_code}")
    client.subscribe(TOPIC)
    client.subscribe(TOPIC_OCC)                 # NEW

def store_occ(d: dict):
    """Vision payload -> occupancy table. cents kept as raw JSON string."""
    t = d.get("t")
    row = (d.get("node"), d.get("occ"), d.get("occ_med"), d.get("occ_max"),
           d.get("occ_last"), json.dumps(d.get("c", [])), d.get("w"), d.get("n"))
    if t:
        conn.execute(
            "INSERT INTO occupancy(ts,node,occ,occ_med,occ_max,occ_last,cents,w,n) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (t, *row))
    else:
        conn.execute(
            "INSERT INTO occupancy(node,occ,occ_med,occ_max,occ_last,cents,w,n) "
            "VALUES(?,?,?,?,?,?,?,?)", row)
    conn.commit()
    print(f"saved occ: {d}")

def on_message(client, userdata, msg):
    try:
        d = json.loads(msg.payload.decode())
    except Exception as e:
        print(f"parse failed: {e}")
        return
    if msg.topic.endswith("/occ"):              # NEW: vision branch
        store_occ(d)
        return
    vals = [d.get(c) for c in COLS]
    t = d.get("t")
    cols_sql = ",".join(COLS)
    ph = ",".join(["?"] * len(COLS))
    if t:
        conn.execute(
            f"INSERT INTO readings(ts,node,{cols_sql},n) VALUES(?,?,{ph},?)",
            (t, d.get("node"), *vals, d.get("n")))
    else:
        conn.execute(
            f"INSERT INTO readings(node,{cols_sql},n) VALUES(?,{ph},?)",
            (d.get("node"), *vals, d.get("n")))
    conn.commit()
    print(f"saved: {d}")

# ---- graceful shutdown ----
def shutdown(signum, frame):
    print("shutting down...")
    try: client.disconnect()
    except Exception: pass
    try: conn.close()
    except Exception: pass
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT,  shutdown)

# ---- MQTT client (TLS + auth) ----
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.username_pw_set(USERNAME, PASSWORD)
client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT, keepalive=60)
client.loop_forever()

