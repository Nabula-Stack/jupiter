"""
Django management command: python manage.py sync_hosts

Runs the background ESXi sync worker that continuously syncs host metadata and VMs.
"""
from django.core.management.base import BaseCommand
from core.run_sync import start_worker


class Command(BaseCommand):
    help = "Start the background ESXi host & VM sync worker"

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.SUCCESS("Starting ESXi Background Sync Worker...")
        )
        try:
            start_worker()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nSync worker stopped by user."))
