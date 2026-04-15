"""
Management command to test WebSocket broadcaster.
Usage: python manage.py test_vm_broadcast
"""

from django.core.management.base import BaseCommand
from manager.models import VirtualMachine
from manager.websocket_service import broadcast_vm_batch
import time


class Command(BaseCommand):
    help = 'Broadcast a test VM status update to WebSocket clients'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--vm-id',
            type=int,
            help='Specific VM ID to broadcast (default: all VMs)',
        )
        parser.add_argument(
            '--count',
            type=int,
            default=1,
            help='Number of updates to send (default: 1)',
        )
        parser.add_argument(
            '--interval',
            type=float,
            default=2,
            help='Seconds between updates (default: 2)',
        )
    
    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('🚀 WebSocket Broadcaster Test'))
        self.stdout.write('-' * 50)
        
        vm_id = options.get('vm_id')
        count = options.get('count', 1)
        interval = options.get('interval', 2)
        
        if vm_id:
            vms = VirtualMachine.objects.filter(id=vm_id)
            if not vms.exists():
                self.stdout.write(self.style.ERROR(f'❌ VM with ID {vm_id} not found'))
                return
        else:
            vms = VirtualMachine.objects.all()
            if not vms.exists():
                self.stdout.write(self.style.ERROR('❌ No VMs found in database'))
                return
        
        self.stdout.write(self.style.SUCCESS(f'✅ Found {vms.count()} VM(s)'))
        self.stdout.write(f'📡 Sending {count} broadcast(s) with {interval}s interval...\n')
        
        for i in range(count):
            self.stdout.write(f'[{i+1}/{count}] Broadcasting...')
            broadcast_vm_batch(vms)
            self.stdout.write(self.style.SUCCESS(f'✅ Broadcast {i+1} sent'))
            
            if i < count - 1:
                time.sleep(interval)
        
        self.stdout.write('\n' + '=' * 50)
        self.stdout.write(self.style.SUCCESS('✅ Test Complete!'))
        self.stdout.write('Monitor WebSocket clients to see the updates.')
