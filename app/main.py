"""FastAPI 应用入口。"""

from __future__ import annotations

from fractions import Fraction

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .converter import (
    convert,
    convert_fraction,
    is_absolute_target,
    to_decimal_str,
)
from .custom_units import build_scope
from .errors import ConversionError, affine_mixing
from .evaluator import evaluate, make_quantity
from .parser import ParsedExpression, canonical, canonical_dim, parse_expression
from .schemas import (
    ConvertRequest,
    ConvertResponse,
    ErrorResponse,
    EvaluateRequest,
    SolveRequest,
)
from .solver import solve
from .units import ALIASES, PREFIXES, REGISTRY

app = FastAPI(
    title="量纲表达式与仿射单位换算 API",
    version=__version__,
    description=(
        "解析单位表达式（乘、除、整数幂），规范化量纲，支持 SI 词头与"
        "摄氏/华氏/开尔文仿射温标；/evaluate 支持带单位的变量公式求值，"
        "/solve 支持用带单位的等式反求一个未知量（未知量须处于一次线性位置），"
        "所有结果均为精确十进制，维度不兼容、未知符号、非法幂、除零、"
        "非法仿射用法、非线性方程、零系数或精度溢出时返回 422。"
    ),
)


# ---------------------------------------------------------------------------
# 异常处理：领域错误与参数校验错误统一返回 422
# ---------------------------------------------------------------------------


@app.exception_handler(ConversionError)
async def conversion_error_handler(_: Request, exc: ConversionError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": {"error": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "error": "invalid_request",
                "message": "请求参数校验失败",
                "errors": errors,
            }
        },
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.post(
    "/convert",
    response_model=ConvertResponse,
    responses={422: {"model": ErrorResponse, "description": "换算失败"}},
    summary="单位换算",
)
def convert_endpoint(req: ConvertRequest) -> ConvertResponse:
    scope = build_scope(req.custom_units)
    src = parse_expression(req.from_unit, scope)
    dst = parse_expression(req.to_unit, scope)
    value = convert(req.value, src, dst)
    return ConvertResponse(
        value=value,
        unit=canonical(dst.terms),
        dimension=canonical_dim(dst.dim),
    )


@app.post(
    "/evaluate",
    response_model=ConvertResponse,
    responses={422: {"model": ErrorResponse, "description": "求值/换算失败"}},
    summary="带单位的变量公式求值并换算到目标单位",
)
def evaluate_endpoint(req: EvaluateRequest) -> ConvertResponse:
    scope = build_scope(req.custom_units)
    # 变量先解析单位并折叠为 SI 口径的精确量；未知单位在此即报 422
    quantities = {
        name: make_quantity(var.value, parse_expression(var.unit, scope))
        for name, var in req.variables.items()
    }
    result = evaluate(req.formula, quantities)
    dst = parse_expression(req.output_unit, scope)

    if result.absolute:
        # 绝对温标变量作为公式唯一项：走仿射换算路径
        if not is_absolute_target(dst):
            raise affine_mixing(
                "绝对温度结果只能换算到绝对温标（degC/degF/K），"
                "不能换算到温差单位或复合单位"
            )
        src = ParsedExpression(dim=result.dim, scale=Fraction(1), terms=(("K", 1),))
        value_fr = convert_fraction(result.value_si, src, dst)
    else:
        # 线性量（含温差）：以 SI 基单位口径构造源表达式
        src = ParsedExpression(dim=result.dim, scale=Fraction(1), terms=())
        value_fr = convert_fraction(result.value_si, src, dst)

    return ConvertResponse(
        value=to_decimal_str(value_fr),
        unit=canonical(dst.terms),
        dimension=canonical_dim(dst.dim),
    )


@app.post(
    "/solve",
    response_model=ConvertResponse,
    responses={422: {"model": ErrorResponse, "description": "求解失败"}},
    summary="用带单位的等式反求一个未知量",
)
def solve_endpoint(req: SolveRequest) -> ConvertResponse:
    scope = build_scope(req.custom_units)
    # 变量先解析单位并折叠为 SI 口径的精确量；未知单位在此即报 422
    quantities = {
        name: make_quantity(var.value, parse_expression(var.unit, scope))
        for name, var in req.variables.items()
    }
    value_si, dim = solve(req.equation, req.solve_for, quantities)
    dst = parse_expression(req.output_unit, scope)
    if dst.affine is not None:
        raise affine_mixing(
            "求解结果只能换算到线性单位，不能换算到绝对温标（degC/degF）"
        )
    if dim is None:
        # 解恒为零且量纲不受方程约束（如 2*x = x）：接受任意线性目标单位
        value_fr = Fraction(0)
    else:
        # 线性量：以 SI 基单位口径构造源表达式
        src = ParsedExpression(dim=dim, scale=Fraction(1), terms=())
        value_fr = convert_fraction(value_si, src, dst)
    return ConvertResponse(
        value=to_decimal_str(value_fr),
        unit=canonical(dst.terms),
        dimension=canonical_dim(dst.dim),
    )


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/units", summary="列出全部支持的单位、词头与别名")
def list_units() -> dict:
    return {
        "units": [
            {
                "symbol": u.symbol,
                "name": u.name,
                "dimension": canonical_dim(u.dim),
                "affine": u.affine,
                "prefixable": u.prefixable,
            }
            for u in REGISTRY.values()
        ],
        "prefixes": sorted(PREFIXES),
        "aliases": ALIASES,
    }


@app.get("/", summary="服务信息")
def root() -> dict[str, str]:
    return {
        "service": "量纲表达式与仿射单位换算 API",
        "version": __version__,
        "docs": "/docs",
    }
