from __future__ import annotations

import threading

from core.proxy_pool import proxy_pool
from domain.proxies import ProxyBulkCreateCommand, ProxyCheckSummary, ProxyCreateCommand, ProxyRecord
from infrastructure.proxies_repository import ProxiesRepository


class ProxiesService:
    def __init__(self, repository: ProxiesRepository | None = None):
        self.repository = repository or ProxiesRepository()

    def list_proxies(self) -> list[dict]:
        return [self._serialize(item) for item in self.repository.list()]

    def create_proxy(self, command: ProxyCreateCommand) -> dict | None:
        item = self.repository.create(command)
        return self._serialize(item) if item else None

    def bulk_create_proxies(self, command: ProxyBulkCreateCommand) -> dict:
        added = self.repository.bulk_create(command.proxies, command.region)
        return {"added": added}

    def delete_proxy(self, proxy_id: int) -> dict:
        return {"ok": self.repository.delete(proxy_id)}

    def toggle_proxy(self, proxy_id: int) -> dict | None:
        value = self.repository.toggle(proxy_id)
        if value is None:
            return None
        return {"is_active": value}

    def trigger_check(self) -> dict:
        threading.Thread(target=proxy_pool.check_all, daemon=True, name="proxy-check").start()
        return {"message": "检测任务已启动"}

    @staticmethod
    def _serialize(item: ProxyRecord) -> dict:
        return {
            "id": item.id,
            "url": item.url,
            "region": item.region,
            "success_count": item.success_count,
            "fail_count": item.fail_count,
            "is_active": item.is_active,
            "last_checked": item.last_checked,
        }
