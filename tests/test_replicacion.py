"""
Replicación de bloques y alta disponibilidad de los datos (Hito 3): informes
de los DataNodes, re-replicación tras caídas, réplicas sobrantes, órdenes
vencidas, réplicas corruptas, conciliación de inventario y estado del
clúster.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.cluster_auth import decode_cluster_token
from app.config import settings
from app.models import Block, BlockReplica, DataNode, ReplicationTask
from app.replication import check_replication
from tests.conftest import as_user
from tests.helpers import cs, heartbeat, register_n, report_stored, store_block

ANA = as_user("ana")


@pytest.fixture()
def api4(api):
    register_n(api, 4)
    api.headers.update(ANA)
    return api


def _write(api, path="/a.bin", size=10, nodes=None):
    plan = api.post(f"/files{path}/allocate", json={"size_bytes": size, "block_size": 10}).json()
    confirmations = [store_block(api, b, nodes=nodes) for b in plan["blocks"]]
    resp = api.post(f"/files{path}/confirm", json={"blocks": confirmations, "lease_id": plan["lease_id"]})
    assert resp.status_code == 200, resp.text
    return plan


def _kill(db, node_id):
    node = db.query(DataNode).filter_by(node_id=node_id).one()
    node.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=settings.heartbeat_ttl_seconds + 5)
    db.commit()


def _stored_nodes(db, block_id):
    block = db.query(Block).filter_by(block_id=block_id).one()
    return sorted(r.datanode.node_id for r in block.replicas if r.state == "stored")


def _execute_replication(api, db):
    """Simula a los DataNodes: cada origen recibe sus órdenes en el heartbeat
    y 'copia' el bloque (el destino lo informa)."""
    copied = []
    for node in db.query(DataNode).all():
        for command in heartbeat(api, node.node_id).json()["replicate"]:
            block = db.query(Block).filter_by(block_id=command["block_id"]).one()
            for target in command["targets"]:
                report_stored(api, target["node_id"], block.block_id, block.checksum, block.size_bytes)
                copied.append((node.node_id, target["node_id"]))
    return copied


def test_un_bloque_confirmado_con_una_sola_replica_se_completa(api4, db):
    plan = _write(api4, nodes=["dn-0"])
    block_id = plan["blocks"][0]["block_id"]
    report = check_replication(db)
    db.commit()
    assert report.scheduled == 2
    copied = _execute_replication(api4, db)
    assert {source for source, _ in copied} == {"dn-0"}
    assert len(_stored_nodes(db, block_id)) == 3
    assert db.query(ReplicationTask).count() == 0
    assert check_replication(db).under_replicated == 0


def test_la_orden_de_copia_trae_un_token_de_escritura_para_el_destino(api4, db):
    _write(api4, nodes=["dn-0"])
    check_replication(db)
    db.commit()
    command = heartbeat(api4, "dn-0").json()["replicate"][0]
    payload = decode_cluster_token(command["token"], "block")
    assert payload["op"] == "write"
    assert [p["node_id"] for p in payload["pipe"]] == [command["targets"][0]["node_id"]]
    # Una orden ya enviada no se repite en el heartbeat siguiente.
    assert heartbeat(api4, "dn-0").json()["replicate"] == []


def test_si_muere_un_datanode_sus_bloques_se_re_replican(api4, db):
    plan = _write(api4)  # cadena dn-0, dn-1, dn-2
    block_id = plan["blocks"][0]["block_id"]
    assert _stored_nodes(db, block_id) == ["dn-0", "dn-1", "dn-2"]
    _kill(db, "dn-1")
    assert check_replication(db).scheduled == 1
    db.commit()
    copied = _execute_replication(api4, db)
    assert [target for _, target in copied] == ["dn-3"]
    assert _stored_nodes(db, block_id) == ["dn-0", "dn-1", "dn-2", "dn-3"]  # dn-1 sigue listado pero muerto
    assert check_replication(db).under_replicated == 0


def test_si_el_nodo_vuelve_se_quita_la_replica_sobrante(api4, db):
    plan = _write(api4)
    block_id = plan["blocks"][0]["block_id"]
    _kill(db, "dn-1")
    check_replication(db)
    db.commit()
    _execute_replication(api4, db)
    heartbeat(api4, "dn-1")  # vuelve
    report = check_replication(db)
    db.commit()
    assert report.removed_excess == 1
    assert len(_stored_nodes(db, block_id)) == 3


def test_una_orden_que_vence_se_reprograma(api4, db, monkeypatch):
    _write(api4, nodes=["dn-0"])
    check_replication(db)
    db.commit()
    for task in db.query(ReplicationTask).all():
        task.created_at = datetime.now(timezone.utc) - timedelta(seconds=settings.replication_task_timeout + 1)
    db.commit()
    expired_targets = {t.target.node_id for t in db.query(ReplicationTask).all()}
    report = check_replication(db)
    db.commit()
    assert report.expired_tasks == 2
    # La copia vencida pudo quedar a medias: su destino se manda limpiar y no
    # se reutiliza hasta que acuse el borrado. Mientras, se usa el nodo libre.
    assert report.scheduled == 1
    for node_id in expired_targets:
        deletions = heartbeat(api4, node_id).json()["delete_blocks"]
        heartbeat(api4, node_id, deleted=deletions)
    assert check_replication(db).scheduled == 1


def test_sin_nodos_libres_no_se_programa_nada(api, db):
    register_n(api, 3)
    api.headers.update(ANA)
    _write(api)
    _kill(db, "dn-2")
    report = check_replication(db)
    # Con 2 nodos vivos el factor efectivo es 2 y ya está cumplido.
    assert report.scheduled == 0
    assert report.under_replicated == 0


def test_bloque_sin_ninguna_replica_viva_cuenta_como_perdido(api4, db):
    plan = _write(api4)
    for node in ("dn-0", "dn-1", "dn-2"):
        _kill(db, node)
    assert check_replication(db).missing == 1
    status = api4.get("/cluster/status").json()
    assert status["missing"] == 1
    # El plan de lectura igual devuelve las réplicas (muertas): el cliente
    # recibirá un error claro, nunca datos corruptos.
    read = api4.get("/files/a.bin/blocks").json()["blocks"][0]
    assert len(read["datanodes"]) == 3
    assert plan


def test_informe_con_checksum_distinto_descarta_esa_replica(api4, db):
    plan = _write(api4, nodes=["dn-0"])
    block = plan["blocks"][0]
    report_stored(api4, "dn-3", block["block_id"], cs("otra-cosa"), block["size_bytes"])
    assert _stored_nodes(db, block["block_id"]) == ["dn-0"]
    assert block["block_id"] in heartbeat(api4, "dn-3").json()["delete_blocks"]


def test_replica_corrupta_informada_se_quita_y_se_re_replica(api4, db):
    plan = _write(api4)
    block_id = plan["blocks"][0]["block_id"]
    resp = api4.post(
        "/datanodes/heartbeat",
        json={"node_id": "dn-1", "corrupt_blocks": [block_id]},
        headers=__import__("tests.helpers", fromlist=["node_headers"]).node_headers("dn-1"),
    )
    assert resp.status_code == 200
    assert _stored_nodes(db, block_id) == ["dn-0", "dn-2"]
    assert check_replication(db).scheduled == 1


def test_informes_atrasados_en_el_heartbeat(api4, db):
    plan = api4.post("/files/a.bin/allocate", json={"size_bytes": 10}).json()
    block = plan["blocks"][0]
    heartbeat(
        api4,
        "dn-0",
        received_blocks=[{"block_id": block["block_id"], "checksum": cs(1), "size_bytes": 10}],
    )
    assert _stored_nodes(db, block["block_id"]) == ["dn-0"]


def test_inventario_concilia_replicas_perdidas_y_huerfanas(api4, db):
    plan = _write(api4)
    block = plan["blocks"][0]
    from tests.helpers import node_headers

    resp = api4.post(
        "/datanodes/block-report",
        json={
            "node_id": "dn-1",
            "blocks": [{"block_id": "huerfano", "checksum": cs("h"), "size_bytes": 1}],
        },
        headers=node_headers("dn-1"),
    )
    assert resp.json() == {"known": 0, "added": 0, "lost": 1, "orphans": 1}
    assert "dn-1" not in _stored_nodes(db, block["block_id"])
    assert "huerfano" in heartbeat(api4, "dn-1").json()["delete_blocks"]
    assert check_replication(db).scheduled == 1


def test_inventario_suma_replicas_que_el_controlnode_no_conocia(api4, db):
    plan = _write(api4, nodes=["dn-0"])
    block = plan["blocks"][0]
    from tests.helpers import node_headers

    resp = api4.post(
        "/datanodes/block-report",
        json={"node_id": "dn-3", "blocks": [{"block_id": block["block_id"], "checksum": cs(block["block_id"]), "size_bytes": 10}]},
        headers=node_headers("dn-3"),
    )
    assert resp.json()["added"] == 1
    assert _stored_nodes(db, block["block_id"]) == ["dn-0", "dn-3"]


def test_estado_del_cluster(api4, db):
    _write(api4)
    status = api4.get("/cluster/status").json()
    assert status["replication_factor"] == 3
    assert status["blocks"] == 1
    assert sorted(n["blocks"] for n in status["datanodes"]) == [0, 1, 1, 1]
    assert status["controlnode"]["role"] == "leader"


def test_borrar_un_archivo_cancela_sus_ordenes_de_copia(api4, db):
    _write(api4, nodes=["dn-0"])
    check_replication(db)
    db.commit()
    assert db.query(ReplicationTask).count() == 2
    api4.request("DELETE", "/fs/rm", json={"path": "/a.bin"})
    assert db.query(ReplicationTask).count() == 0
    assert db.query(BlockReplica).count() == 0
