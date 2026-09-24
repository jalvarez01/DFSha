import hashlib

from tests.conftest import make_token


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_put_then_get_roundtrip(client):
    put_resp = client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"hola bloque")
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["size_bytes"] == len(b"hola bloque")

    get_resp = client.get("/blocks/blk-1")
    assert get_resp.status_code == 200
    assert get_resp.content == b"hola bloque"


def test_get_missing_block_is_404(client):
    resp = client.get("/blocks/no-existe")
    assert resp.status_code == 404


def test_delete_block(client):
    client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"data")
    del_resp = client.delete("/blocks/blk-1")
    assert del_resp.status_code == 200
    assert client.get("/blocks/blk-1").status_code == 404


def test_delete_missing_block_is_404(client):
    assert client.delete("/blocks/no-existe").status_code == 404


def test_delete_file_blocks(client):
    client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"a")
    client.put("/blocks/blk-2", params={"file_id": "7"}, content=b"b")
    resp = client.delete("/files/7/blocks")
    assert resp.status_code == 200
    assert resp.json()["blocks_deleted"] == 2
    assert client.get("/blocks/blk-1").status_code == 404
    assert client.get("/blocks/blk-2").status_code == 404


# --------------------------------------------------------------------------
# Autenticación (JWT propagado desde el ControlNode)
# --------------------------------------------------------------------------
def test_health_no_exige_token(anon_client):
    assert anon_client.get("/health").status_code == 200


def test_sin_token_es_401(anon_client):
    assert anon_client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"x").status_code == 401
    assert anon_client.get("/blocks/blk-1").status_code == 401
    assert anon_client.delete("/blocks/blk-1").status_code == 401
    assert anon_client.delete("/files/7/blocks").status_code == 401


def test_token_firmado_con_otro_secreto_es_401(anon_client):
    headers = {"Authorization": f"Bearer {make_token(secret='otro-secreto')}"}
    resp = anon_client.get("/blocks/blk-1", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_token"


def test_token_expirado_es_401(anon_client):
    import datetime as dt

    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    headers = {"Authorization": f"Bearer {make_token(exp=past)}"}
    assert anon_client.get("/blocks/blk-1", headers=headers).status_code == 401


# --------------------------------------------------------------------------
# Seguridad del storage: ids validados (sin path traversal)
# --------------------------------------------------------------------------
def test_file_id_con_path_traversal_es_400(client, tmp_path):
    for bad in ("..", "../..", "a/b", "a b", ""):
        resp = client.put("/blocks/blk-1", params={"file_id": bad}, content=b"x")
        assert resp.status_code in (400, 422), bad
    # Nada se escribió fuera (ni dentro) del storage.
    assert list(tmp_path.iterdir()) == []


def test_block_id_invalido_es_400(client):
    resp = client.get("/blocks/..%2e")
    assert resp.status_code in (400, 404)
    resp = client.put("/blocks/blk%2E%2E", params={"file_id": "7"}, content=b"x")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_id"


def test_delete_file_blocks_con_traversal_es_400_y_no_borra_nada(client, tmp_path):
    victim = tmp_path / "victima"
    victim.mkdir()
    resp = client.delete("/files/%2E%2E/blocks")
    assert resp.status_code == 400
    assert victim.exists()


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------
def test_put_con_checksum_correcto_se_acepta(client):
    data = b"bloque verificado"
    resp = client.put(
        "/blocks/blk-1",
        params={"file_id": "7"},
        content=data,
        headers={"X-Checksum-Sha256": hashlib.sha256(data).hexdigest()},
    )
    assert resp.status_code == 200
    assert resp.json()["checksum"] == hashlib.sha256(data).hexdigest()


def test_put_con_checksum_incorrecto_se_rechaza_y_no_se_guarda(client):
    resp = client.put(
        "/blocks/blk-1",
        params={"file_id": "7"},
        content=b"contenido",
        headers={"X-Checksum-Sha256": "0" * 64},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "checksum_mismatch"
    assert client.get("/blocks/blk-1").status_code == 404


def test_put_rechazado_no_pisa_la_version_anterior(client):
    client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"buena")
    client.put(
        "/blocks/blk-1",
        params={"file_id": "7"},
        content=b"mala",
        headers={"X-Checksum-Sha256": "0" * 64},
    )
    assert client.get("/blocks/blk-1").content == b"buena"


def test_get_devuelve_checksum_y_tamano(client):
    data = b"x" * 200_000  # varios chunks
    client.put("/blocks/blk-1", params={"file_id": "7"}, content=data)
    resp = client.get("/blocks/blk-1")
    assert resp.content == data
    assert resp.headers["x-checksum-sha256"] == hashlib.sha256(data).hexdigest()
    assert resp.headers["content-length"] == str(len(data))


def test_get_de_bloque_corrupto_en_disco_es_500(client, tmp_path):
    client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"original")
    (tmp_path / "7" / "blk-1").write_bytes(b"alterado")
    resp = client.get("/blocks/blk-1")
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "corrupt_block"
