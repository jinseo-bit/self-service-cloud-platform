"""Environment records in DynamoDB. Large JSON blobs are stored as strings (no Decimal juggling)."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets

import boto3
from boto3.dynamodb.conditions import Attr, Key

JSON_FIELDS = ("input", "recommendation", "outputs", "checks", "verdict", "artifact", "failure", "admission", "revisions",
               "budget_override", "origin")


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_env_id() -> str:
    return "env-" + secrets.token_hex(4)


class Store:
    def __init__(self, table_name: str | None = None, resource=None):
        self.table = (resource or boto3.resource("dynamodb")).Table(table_name or os.environ["ENV_TABLE"])

    @staticmethod
    def _dump(item: dict) -> dict:
        return {k: (json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v)
                for k, v in item.items() if v is not None}

    @staticmethod
    def _load(item: dict | None) -> dict | None:
        if item is None:
            return None
        out = dict(item)
        for k in JSON_FIELDS:
            if isinstance(out.get(k), str):
                out[k] = json.loads(out[k])
        for k in ("cost_upper_usd", "current_revision", "previous_revision", "delete_attempts"):
            if out.get(k) is not None:  # DynamoDB returns Decimal; the API and UI compare these as integers
                out[k] = int(out[k])
        return out

    def put(self, item: dict) -> None:
        self.table.put_item(Item=self._dump(item), ConditionExpression=Attr("env_id").not_exists())

    def get(self, env_id: str) -> dict | None:
        return self._load(self.table.get_item(Key={"env_id": env_id}, ConsistentRead=True).get("Item"))

    def update(self, env_id: str, *, expect_status: tuple[str, ...] | None = None, event: str | None = None, **fields) -> dict:
        fields["updated_at"] = now()
        names, values, sets = {}, {}, []
        for i, (k, v) in enumerate(fields.items()):
            names[f"#u{i}"] = k
            values[f":u{i}"] = json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v
            sets.append(f"#u{i} = :u{i}")  # not :v/#n — boto3 condition builder uses those
        expr = "SET " + ", ".join(sets)
        if event:
            names["#ev"] = "event_log"
            values[":ev"] = [{"at": fields["updated_at"], "status": fields.get("status", ""), "message": event}]
            values[":empty"] = []
            expr += ", #ev = list_append(if_not_exists(#ev, :empty), :ev)"
        kwargs = dict(Key={"env_id": env_id}, UpdateExpression=expr, ExpressionAttributeNames=names,
                      ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
        cond = Attr("env_id").exists()
        if expect_status:
            cond = cond & Attr("status").is_in(list(expect_status))
        kwargs["ConditionExpression"] = cond
        return self._load(self.table.update_item(**kwargs)["Attributes"])

    def list_owner(self, owner_sub: str) -> list[dict]:
        items, kw = [], dict(IndexName="by-owner", KeyConditionExpression=Key("owner_sub").eq(owner_sub))
        while True:
            r = self.table.query(**kw)
            items += r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return [self._load(i) for i in items]

    def list_all(self) -> list[dict]:
        items, kw = [], {}
        while True:
            r = self.table.scan(**kw)
            items += r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return [self._load(i) for i in items]
