# =====================================================================
# Quick check: is innodb_flush_log_at_trx_commit consistent across
# all 3 PXC nodes right now? (Reviewer 1, point 7 investigation)
#
# If the 3 values differ, that's direct evidence the controller has
# only ever been reaching one node at a time -- confirming the bug
# described in mysql_multinode_patch_point7.py.
#
# Run with a port-forward active to EACH pod individually (not the
# load-balanced Service, which is exactly the thing we're checking
# isn't giving us a consistent view):
#
#   kubectl port-forward pod/<pxc-pod-0> 33061:3306 -n default
#   kubectl port-forward pod/<pxc-pod-1> 33062:3306 -n default
#   kubectl port-forward pod/<pxc-pod-2> 33063:3306 -n default
#
# (replace <pxc-pod-N> with your actual pod names, e.g.
#  pxc-rpo-experiment-pxc-0/1/2 if using tonight's Percona Operator
#  deployment, or mysql-pxc-0/1/2 if using the repo's plain StatefulSet)
# =====================================================================

import mysql.connector

NODES = [
    {"name": "node-0", "host": "127.0.0.1", "port": 33061},
    {"name": "node-1", "host": "127.0.0.1", "port": 33062},
    {"name": "node-2", "host": "127.0.0.1", "port": 33063},
]

USER = "root"
PASSWORD = "ChangeMeRootPW!"   # adjust to your actual root password

print(f"{'Node':<10} {'innodb_flush_log_at_trx_commit':<35} {'wsrep_cluster_size':<20}")
print("-" * 65)

values_seen = set()
for node in NODES:
    try:
        conn = mysql.connector.connect(
            host=node["host"], port=node["port"],
            user=USER, password=PASSWORD, connection_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("SHOW GLOBAL VARIABLES LIKE 'innodb_flush_log_at_trx_commit'")
        flush_val = cur.fetchone()[1]
        cur.execute("SHOW GLOBAL STATUS LIKE 'wsrep_cluster_size'")
        cluster_size = cur.fetchone()
        cluster_size = cluster_size[1] if cluster_size else "N/A"
        print(f"{node['name']:<10} {flush_val:<35} {cluster_size:<20}")
        values_seen.add(flush_val)
        conn.close()
    except Exception as e:
        print(f"{node['name']:<10} ERROR: {e}")

print()
if len(values_seen) > 1:
    print("*** INCONSISTENT across nodes -- confirms the single-connection bug. ***")
    print("*** The controller has NOT been applying its intended durability   ***")
    print("*** setting to all 3 Galera nodes uniformly.                       ***")
elif len(values_seen) == 1:
    print("All 3 nodes currently agree -- inconclusive on its own (they may")
    print("simply not have diverged yet, or a previous run happened to land")
    print("consistently). This does not rule out the bug; it only means this")
    print("particular snapshot doesn't show it. The code-level fix is still")
    print("needed regardless of this result.")
