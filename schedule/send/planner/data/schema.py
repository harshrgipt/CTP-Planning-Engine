"""Canonical polars schemas for warehouse tables. All timestamps stored as pl.Datetime."""
from __future__ import annotations

import polars as pl


CURING_SCHEMA = {
    "iD": pl.Int64,
    "wcID": pl.Int64,
    "gtbarCode": pl.Utf8,
    "pressbarCode": pl.Utf8,
    "serialNo": pl.Utf8,
    "recipeID": pl.Int64,
    "mouldNo": pl.Utf8,
    "manningID": pl.Int64,
    "dtandTime": pl.Utf8,
    "updateStatus": pl.Utf8,
    "cycleUpdate": pl.Utf8,
    "statuscritical": pl.Utf8,
    "statusMinor": pl.Utf8,
    "cycleStart": pl.Utf8,
    "BladdercodeLH": pl.Utf8,
    "BladderCodeRH": pl.Utf8,
    "MouldCodeLH": pl.Utf8,
    "MouldCodeRH": pl.Utf8,
    "BladderCountLH": pl.Int64,
    "BladderCountRH": pl.Int64,
    "MouldCountLH": pl.Int64,
    "MouldCountRH": pl.Int64,
}


BUILDING_OUTPUT_SCHEMA = {
    "iD": pl.Int64,
    "machineCode": pl.Utf8,
    "productionID": pl.Utf8,
    "itemCode": pl.Utf8,
    "quantity": pl.Float64,
    "dtandTime": pl.Utf8,
    "QualityStatus": pl.Utf8,
    "ConsumeStatus": pl.Utf8,
}


CONSUMPTION_SCHEMA = {
    "ID": pl.Int64,
    "machineCode": pl.Utf8,
    "productionID": pl.Utf8,
    "consumptionItemCode": pl.Utf8,
    "consumedQuantity": pl.Float64,
    "dtandTime": pl.Utf8,
    "syncStatus": pl.Utf8,
}
