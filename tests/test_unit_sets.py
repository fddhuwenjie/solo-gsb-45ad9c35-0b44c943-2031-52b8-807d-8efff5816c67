"""已发布可复用定义集（POST /unit-sets + unit_set_id）的端到端测试。

覆盖：
- 发布：1～6 项、按现有规则校验并展开全部条目，任一项非法则 422 且
  不留下可引用的定义集；成功返回服务生成的标识，内容不可改；
- /convert、/evaluate、/solve 按 unit_set_id 选用定义集，源/目标/变量
  单位共用同一份定义；unit_set_id 与 custom_units 同时出现、标识不存在
  均整次返回 422；
- 不同集合允许同名符号有不同含义；并发调用互不污染、全局注册表不变；
- 持久化：重新载入（模拟容器重建）后已发布集合仍可使用；
- 未选用定义集时三个接口的原有行为不变。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.unit_sets import UnitSetStore
from app.units import REGISTRY

client = TestClient(app)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """每个测试使用独立的临时持久化文件，并替换应用级存储。"""
    s = UnitSetStore(tmp_path / "unit_sets.json")
    monkeypatch.setattr(main, "unit_set_store", s)
    return s


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def publish(units):
    return client.post("/unit-sets", json={"units": units})


def publish_ok(units):
    r = publish(units)
    assert r.status_code == 200, r.text
    set_id = r.json()["unit_set_id"]
    assert set_id.startswith("us_")
    return set_id


def assert_ok(resp, value, unit, dimension):
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"value": value, "unit": unit, "dimension": dimension}


def assert_err(resp, code):
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


# ---------------------------------------------------------------------------
# 发布：成功、响应内容、标识
# ---------------------------------------------------------------------------


def test_publish_returns_generated_id_and_echoes_definitions(store):
    r = publish([cu("ft", "0.3048", "m")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit_set_id"].startswith("us_")
    assert body["units"] == [{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}]


def test_publish_chain_with_forward_reference(store):
    # 与请求级 custom_units 相同：允许前向引用，按名称解析、与声明顺序无关
    set_id = publish_ok([cu("furlong", "10", "chain"), cu("chain", "22", "m")])
    r = client.post(
        "/convert",
        json={"value": "2", "from": "furlong", "to": "m", "unit_set_id": set_id},
    )
    assert_ok(r, "440", "m", "m")


def test_publish_count_limits(store):
    assert_err(publish([]), "invalid_request")
    seven = [cu(f"u{i}", "1", "m") for i in range(7)]
    assert_err(publish(seven), "invalid_request")
    assert len(store) == 0
    # 边界：1 项与 6 项均可发布
    publish_ok([cu("u1", "1", "m")])
    publish_ok([cu(f"v{i}", "1", "m") for i in range(6)])
    assert len(store) == 2


def test_publish_malformed_body(store):
    assert_err(client.post("/unit-sets", json={}), "invalid_request")
    assert_err(client.post("/unit-sets", json={"units": "not-a-list"}), "invalid_request")
    assert_err(
        client.post("/unit-sets", json={"units": [{"symbol": "a", "factor": "1"}]}),
        "invalid_request",
    )
    assert len(store) == 0


def test_publish_invalid_definitions_leave_no_set(store):
    # 任一项非法则整次 422，不留下可引用的定义集（存储为空、不落盘）
    bad_sets = [
        [cu("m", "1", "m")],  # 与内置单位冲突
        [cu("km", "1", "m")],  # 与可由词头解析的符号冲突
        [cu("aa", "1", "m"), cu("aa", "2", "m")],  # 重名
        [cu("aa", "1", "bb"), cu("bb", "1", "aa")],  # 循环引用
        [cu("aa", "1", "aa")],  # 自引用
        [cu("aa", "1", "K")],  # 禁引裸 K
        [cu("aa", "1", "degC")],  # 禁引 degC
        [cu("aa", "0", "m")],  # 非正比例
        [cu("aa", "1", "m//s")],  # base_unit 语法错误
        [cu("good", "1", "m"), cu("bad", "1", "zzz")],  # 任一项非法即整组失败
    ]
    for units in bad_sets:
        r = publish(units)
        assert r.status_code == 422, (units, r.text)
    assert len(store) == 0
    assert not (store._path).exists()


def test_publish_unknown_reference_error_code(store):
    assert_err(publish([cu("aa", "1", "zzz")]), "unknown_symbol")


def test_republish_same_content_gets_new_id(store):
    # 已发布内容不可改：没有更新操作，重复发布得到新标识，两个集合各自可用
    id1 = publish_ok([cu("foo", "2", "m")])
    id2 = publish_ok([cu("foo", "2", "m")])
    assert id1 != id2
    for set_id in (id1, id2):
        r = client.post(
            "/convert",
            json={"value": "1", "from": "foo", "to": "m", "unit_set_id": set_id},
        )
        assert_ok(r, "2", "m", "m")


# ---------------------------------------------------------------------------
# 三个接口按 unit_set_id 选用定义集
# ---------------------------------------------------------------------------


def test_convert_with_unit_set(store):
    set_id = publish_ok([cu("ft", "0.3048", "m")])
    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m", "unit_set_id": set_id},
    )
    assert_ok(r, "3.048", "m", "m")
    r = client.post(
        "/convert",
        json={"value": "3.048", "from": "m", "to": "ft", "unit_set_id": set_id},
    )
    assert_ok(r, "10", "ft", "m")


def test_convert_with_unit_set_canonical_output(store):
    # 源、目标共用同一份定义；规范输出保留自定义符号，量纲仍为 SI 基本量纲
    set_id = publish_ok([cu("bar", "100000", "Pa")])
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "N*bar", "to": "kg*bar*m/s^2",
            "unit_set_id": set_id,
        },
    )
    assert_ok(r, "1", "bar*kg*m/s^2", "kg^2/s^4")


def test_evaluate_with_unit_set(store):
    # 变量单位与 output_unit 也按同一份定义解析
    set_id = publish_ok([cu("dozen", "12", "1")])
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "a": {"value": "2", "unit": "dozen"},
                "b": {"value": "3", "unit": "dozen"},
            },
            "formula": "a*b",
            "output_unit": "dozen^2",
            "unit_set_id": set_id,
        },
    )
    assert_ok(r, "6", "dozen^2", "1")


def test_solve_with_unit_set(store):
    set_id = publish_ok([cu("rod", "5.0292", "m")])
    r = client.post(
        "/solve",
        json={
            "variables": {"a": {"value": "3", "unit": "rod"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
            "unit_set_id": set_id,
        },
    )
    assert_ok(r, "15.0876", "m", "m")


def test_unit_set_symbols_get_no_prefixes(store):
    # 与请求级 custom_units 相同：自定义符号不自动获得词头
    set_id = publish_ok([cu("foo", "2", "1")])
    r = client.post(
        "/convert",
        json={"value": "1", "from": "kfoo", "to": "1", "unit_set_id": set_id},
    )
    assert_err(r, "unknown_symbol")


def test_unit_set_does_not_leak_into_plain_requests(store):
    publish_ok([cu("foo", "2", "m")])
    # 未选用定义集的请求看不到集合中的符号
    r = client.post("/convert", json={"value": "1", "from": "foo", "to": "m"})
    assert_err(r, "unknown_symbol")


# ---------------------------------------------------------------------------
# 两种方式同时出现 / 标识不存在：整次 422
# ---------------------------------------------------------------------------


def test_unit_set_and_custom_units_together_rejected(store):
    set_id = publish_ok([cu("ft", "0.3048", "m")])
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "ft", "to": "m",
            "unit_set_id": set_id,
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert_err(r, "invalid_request")
    r = client.post(
        "/evaluate",
        json={
            "variables": {"a": {"value": "1", "unit": "m"}},
            "formula": "a",
            "output_unit": "m",
            "unit_set_id": set_id,
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert_err(r, "invalid_request")
    r = client.post(
        "/solve",
        json={
            "equation": "x = 1",
            "solve_for": "x",
            "output_unit": "m",
            "unit_set_id": set_id,
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert_err(r, "invalid_request")


def test_unknown_unit_set_id_rejected(store):
    missing = "us_0000000000000000"
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "m", "unit_set_id": missing},
    )
    assert_err(r, "unknown_unit_set")
    r = client.post(
        "/evaluate",
        json={
            "variables": {"a": {"value": "1", "unit": "m"}},
            "formula": "a",
            "output_unit": "m",
            "unit_set_id": missing,
        },
    )
    assert_err(r, "unknown_unit_set")
    r = client.post(
        "/solve",
        json={
            "equation": "x = 1", "solve_for": "x", "output_unit": "m",
            "unit_set_id": missing,
        },
    )
    assert_err(r, "unknown_unit_set")


def test_null_unit_set_id_is_equivalent_to_absent(store):
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "cm", "unit_set_id": None},
    )
    assert_ok(r, "100", "cm", "m")


# ---------------------------------------------------------------------------
# 集合间隔离、并发与全局注册表
# ---------------------------------------------------------------------------


def test_same_symbol_different_meanings_across_sets(store):
    # 不同定义集允许同名符号有不同含义
    id2 = publish_ok([cu("foo", "2", "m")])
    id7 = publish_ok([cu("foo", "7", "m")])
    r = client.post(
        "/convert", json={"value": "1", "from": "foo", "to": "m", "unit_set_id": id2}
    )
    assert_ok(r, "2", "m", "m")
    r = client.post(
        "/convert", json={"value": "1", "from": "foo", "to": "m", "unit_set_id": id7}
    )
    assert_ok(r, "7", "m", "m")


def test_concurrent_calls_with_different_sets(store):
    id2 = publish_ok([cu("foo", "2", "m")])
    id7 = publish_ok([cu("foo", "7", "m")])

    def call(set_id):
        r = client.post(
            "/convert",
            json={"value": "1", "from": "foo", "to": "m", "unit_set_id": set_id},
        )
        assert r.status_code == 200, r.text
        return r.json()["value"]

    ids = [id2] * 25 + [id7] * 25
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(call, ids))
    assert results.count("2") == 25
    assert results.count("7") == 25


def test_concurrent_publish_and_use(store):
    def publish_and_convert(i):
        set_id = publish_ok([cu("foo", str(i + 1), "m")])
        r = client.post(
            "/convert",
            json={"value": "1", "from": "foo", "to": "m", "unit_set_id": set_id},
        )
        assert r.status_code == 200, r.text
        return r.json()["value"]

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(publish_and_convert, range(20)))
    assert sorted(results, key=float) == [str(i + 1) for i in range(20)]
    assert len(store) == 20


def test_global_registry_is_not_mutated(store):
    set_id = publish_ok([cu("ft", "0.3048", "m")])
    client.post(
        "/convert",
        json={"value": "1", "from": "ft", "to": "m", "unit_set_id": set_id},
    )
    assert "ft" not in REGISTRY
    assert "foo" not in REGISTRY


# ---------------------------------------------------------------------------
# 持久化：重新载入（模拟容器重建）后已发布集合仍可使用
# ---------------------------------------------------------------------------


def test_published_sets_survive_reload(store, tmp_path, monkeypatch):
    set_id = publish_ok(
        [cu("furlong", "10", "chain"), cu("chain", "22", "m")]
    )
    # 持久化文件是结构完整的 JSON，factor 保持十进制字符串
    raw = json.loads((tmp_path / "unit_sets.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["sets"][set_id] == [
        {"symbol": "furlong", "factor": "10", "base_unit": "chain"},
        {"symbol": "chain", "factor": "22", "base_unit": "m"},
    ]
    # 模拟服务/容器重启：同一路径重新载入，旧标识仍可使用
    reloaded = UnitSetStore(tmp_path / "unit_sets.json")
    monkeypatch.setattr(main, "unit_set_store", reloaded)
    r = client.post(
        "/convert",
        json={"value": "2", "from": "furlong", "to": "m", "unit_set_id": set_id},
    )
    assert_ok(r, "440", "m", "m")
    # 重启后发布新集合也不会与旧标识冲突
    new_id = publish_ok([cu("ft", "0.3048", "m")])
    assert new_id != set_id
    assert len(reloaded) == 2


def test_store_reloads_exact_factor(store, tmp_path, monkeypatch):
    set_id = publish_ok([cu("ft", "0.3048", "m")])
    reloaded = UnitSetStore(tmp_path / "unit_sets.json")
    monkeypatch.setattr(main, "unit_set_store", reloaded)
    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m", "unit_set_id": set_id},
    )
    assert_ok(r, "3.048", "m", "m")


# ---------------------------------------------------------------------------
# 未选用定义集：三个接口的原有行为与响应不变
# ---------------------------------------------------------------------------


def test_without_unit_set_behaviour_unchanged(store):
    assert_ok(
        client.post("/convert", json={"value": "36", "from": "km/h", "to": "m/s"}),
        "10", "m/s", "m/s",
    )
    assert_ok(
        client.post(
            "/convert",
            json={
                "value": "10", "from": "ft", "to": "m",
                "custom_units": [cu("ft", "0.3048", "m")],
            },
        ),
        "3.048", "m", "m",
    )
    assert_ok(
        client.post(
            "/evaluate",
            json={
                "variables": {
                    "v": {"value": "60", "unit": "km/h"},
                    "t": {"value": "2", "unit": "h"},
                },
                "formula": "v*t",
                "output_unit": "km",
            },
        ),
        "120", "km", "m",
    )
    assert_ok(
        client.post(
            "/solve",
            json={
                "variables": {
                    "F": {"value": "6", "unit": "N"},
                    "m": {"value": "2", "unit": "kg"},
                },
                "equation": "F = m*a",
                "solve_for": "a",
                "output_unit": "m/s^2",
            },
        ),
        "3", "m/s^2", "m/s^2",
    )
