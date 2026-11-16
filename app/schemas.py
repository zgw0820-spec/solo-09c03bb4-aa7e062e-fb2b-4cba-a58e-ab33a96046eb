from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_NODES = 4
MAX_CONTAINERS = 8
MAX_CPU = 100_000

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_label(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field}不能为空")
    if len(value) > 64:
        raise ValueError(f"{field}不能超过 64 个字符")
    if _CONTROL_RE.search(value):
        raise ValueError(f"{field}不能包含控制字符")
    return value


class NodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str
    name: str | None = Field(default=None, max_length=64)
    cpu_capacity: int = Field(gt=0, le=MAX_CPU)

    @field_validator("id")
    @classmethod
    def check_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("节点 id 只能是 1-64 位字母、数字或 . _ : -")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_label(value, "节点名称")


class ContainerIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str
    name: str | None = Field(default=None, max_length=64)
    service: str
    min_ready: int = Field(ge=0, le=MAX_CONTAINERS)
    node_id: str
    cpu_request: int = Field(gt=0, le=MAX_CPU)

    @field_validator("id")
    @classmethod
    def check_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("容器 id 只能是 1-64 位字母、数字或 . _ : -")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_label(value, "容器名称")

    @field_validator("service")
    @classmethod
    def clean_service(cls, value: str) -> str:
        return _clean_label(value, "所属业务")


class TopologyIn(BaseModel):
    """Payload accepted by the dashboard's JSON import and the REST API."""

    model_config = ConfigDict(extra="forbid")

    nodes: Annotated[list[NodeIn], Field(min_length=1, max_length=MAX_NODES)]
    containers: Annotated[list[ContainerIn], Field(max_length=MAX_CONTAINERS)]

    @model_validator(mode="after")
    def check_topology(self) -> "TopologyIn":
        node_ids = [node.id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("节点 id 重复")
        container_ids = [container.id for container in self.containers]
        if len(set(container_ids)) != len(container_ids):
            raise ValueError("容器 id 重复")

        known_nodes = set(node_ids)
        per_node: dict[str, int] = {node_id: 0 for node_id in node_ids}
        service_replicas: dict[str, int] = {}
        service_min_ready: dict[str, int] = {}
        for container in self.containers:
            if container.node_id not in known_nodes:
                raise ValueError(
                    f"容器 {container.id!r} 引用了不存在的节点 {container.node_id!r}"
                )
            per_node[container.node_id] += container.cpu_request
            service_replicas[container.service] = (
                service_replicas.get(container.service, 0) + 1
            )
            previous = service_min_ready.get(container.service)
            if previous is not None and previous != container.min_ready:
                raise ValueError(
                    f"业务 {container.service!r} 的 min_ready 不一致："
                    f"{previous} 与 {container.min_ready}"
                )
            service_min_ready[container.service] = container.min_ready

        for node in self.nodes:
            if per_node[node.id] > node.cpu_capacity:
                raise ValueError(
                    f"节点 {node.id!r} 超出容量：请求 {per_node[node.id]} > "
                    f"容量 {node.cpu_capacity}"
                )
        for service, minimum in service_min_ready.items():
            replicas = service_replicas[service]
            if minimum > replicas:
                raise ValueError(
                    f"业务 {service!r} 要求 min_ready={minimum}，但只定义了 "
                    f"{replicas} 个容器"
                )
        return self


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outcome: Literal["ready", "failed"]
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None
