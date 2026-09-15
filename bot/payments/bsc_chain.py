"""Binance-Peg USDT on BNB Smart Chain; shares strict EVM receipt validation."""
from .polygon_chain import PolygonGateway


class BscGateway(PolygonGateway):
    chain_id = 56
    token_contract = "0x55d398326f99059ff775485246999027b3197955"
    decimals = 18
