from django.apps import AppConfig
from django.conf import settings


class ManagerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'manager'

    def ready(self):
        """
        Reset the WebSocket active-client counter to 0 on server startup.

        This prevents the sync loop from staying permanently 'active' if the
        container crashed while clients were connected (the Redis counter would
        be stuck at a positive value and never drop back to zero).
        """
        try:
            import redis
            r = redis.Redis(
                host=getattr(settings, 'REDIS_HOST', '127.0.0.1'),
                port=int(getattr(settings, 'REDIS_PORT', 6379)),
                decode_responses=True,
            )
            r.set('active_sync_users', 0)
            r.close()
            print("[ManagerConfig] Reset active_sync_users → 0")
        except Exception as exc:
            # Non-fatal: Redis may not be available during migrations or tests.
            print(f"[ManagerConfig] Could not reset active_sync_users: {exc}")
