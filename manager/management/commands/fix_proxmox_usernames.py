"""
Django management command: python manage.py fix_proxmox_usernames

Proxmox REST API requires usernames in user@realm format (e.g. root@pam).
This command scans every active Proxmox host in the database and appends
'@pam' to any username that is missing the realm suffix.

Run this once after upgrading, or whenever you add a Proxmox host with a
bare username (e.g. 'root' → fixed to 'root@pam').
"""
from django.core.management.base import BaseCommand

from manager.models import Host


DEFAULT_REALM = "pam"


class Command(BaseCommand):
    help = "Append @pam realm to Proxmox host usernames that are missing it"

    def add_arguments(self, parser):
        parser.add_argument(
            "--realm",
            default=DEFAULT_REALM,
            help=f"Realm to append when missing (default: {DEFAULT_REALM})",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without saving",
        )

    def handle(self, *args, **options):
        realm = options["realm"]
        dry_run = options["dry_run"]

        proxmox_hosts = Host.objects.filter(
            hypervisor_type=Host.HYPERVISOR_PROXMOX_VE
        )

        if not proxmox_hosts.exists():
            self.stdout.write(self.style.WARNING("No Proxmox hosts found."))
            return

        fixed = 0
        for host in proxmox_hosts:
            if "@" not in (host.username or ""):
                new_username = f"{host.username}@{realm}"
                if dry_run:
                    self.stdout.write(
                        f"  [dry-run] '{host.name}': "
                        f"'{host.username}' → '{new_username}'"
                    )
                else:
                    host.username = new_username
                    host.save(update_fields=["username"])
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  ✅ Fixed '{host.name}': '{host.username}' saved"
                        )
                    )
                fixed += 1
            else:
                self.stdout.write(f"  ✓ '{host.name}': '{host.username}' already correct")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"\nDry run — {fixed} host(s) would be updated.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"\nDone — {fixed} host(s) updated.")
            )
