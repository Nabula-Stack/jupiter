/**
 * Simple WebSocket Live Updates for Django Unfold
 * Updates VM status without interfering with Unfold styling
 */
(function() {
    let socket = null;
    let reconnectAttempts = 0;
    const MAX_RECONNECT = 5;
    const RECONNECT_DELAY = 3000;
    
    function connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = protocol + '//' + window.location.host + '/ws/vms/updates/';
        
        console.log('[VM-Live] Attempting connection to:', url);
        
        try {
            socket = new WebSocket(url);
            socket.onopen = onOpen;
            socket.onmessage = onMessage;
            socket.onerror = onError;
            socket.onclose = onClose;
        } catch(e) {
            console.error('[VM-Live] Connection error:', e);
        }
    }
    
    function onOpen() {
        console.log('[VM-Live] ✅ Connected successfully');
        reconnectAttempts = 0;
        send({type: 'ping'});
        
        // Keep connection alive
        setInterval(function() {
            if (socket && socket.readyState === 1) {
                send({type: 'ping'});
            }
        }, 30000);
    }
    
    function onMessage(event) {
        try {
            const data = JSON.parse(event.data);
            const eventType = data.type || data.event_type || '';
            console.log('[VM-Live] Message received:', eventType || 'unknown', data.vm_name || data.host_name);

            // Support legacy and new event formats.
            const isVmEvent = (
                eventType === 'vm_update' ||
                eventType === 'vm_status_update' ||
                eventType === 'vm_power_state_changed' ||
                eventType === 'vm_modified' ||
                eventType === 'vm_created' ||
                (data.vm_id !== undefined && data.power_state !== undefined)
            );
            const isHostEvent = (
                eventType === 'host_update' ||
                eventType === 'host_status_update' ||
                (data.host_id !== undefined && data.cpu_usage_percent !== undefined)
            );

            if (isVmEvent) {
                console.log('[VM-Live] 🔄 Updating VM:', data.vm_name);
                updateUI(data);
            } else if (isHostEvent) {
                console.log('[VM-Live] 🔄 Updating Host:', data.host_name);
                updateHostUI(data);
            } else if (eventType === 'pong') {
                console.log('[VM-Live] 💚 Pong received');
            }
        } catch(e) {
            console.error('[VM-Live] Parse error:', e);
        }
    }
    
    function onError(error) {
        console.error('[VM-Live] ❌ Error:', error);
    }
    
    function onClose() {
        console.warn('[VM-Live] ⚠️  Disconnected - reconnecting...');
        if (reconnectAttempts < MAX_RECONNECT) {
            reconnectAttempts++;
            const delay = RECONNECT_DELAY * reconnectAttempts;
            console.log('[VM-Live] Reconnect attempt', reconnectAttempts, 'in', delay, 'ms');
            setTimeout(connect, delay);
        } else {
            console.error('[VM-Live] ❌ Max reconnect attempts reached');
        }
    }
    
    function send(msg) {
        if (socket && socket.readyState === 1) {
            try { 
                socket.send(JSON.stringify(msg));
            } catch(e) {
                console.error('[VM-Live] Send error:', e);
            }
        } else {
            console.warn('[VM-Live] ⚠️  Cannot send - socket not ready');
        }
    }
    
    function updateUI(data) {
        // Update power status
        const statusEl = document.getElementById('vm-status-' + data.vm_id);
        if (statusEl && data.power_state !== undefined && data.power_state !== null) {
            const state = String(data.power_state).toLowerCase();
            const isOn = state.includes('on');
            const isSuspended = state.includes('suspend');
            const color = isOn ? '#10b981' : (isSuspended ? '#f59e0b' : '#ef4444');
            const text = isOn ? 'Running' : (isSuspended ? 'Suspended' : 'Stopped');
            console.log('[VM-Live] Updating status for VM', data.vm_id, ':', text);
            statusEl.textContent = '● ' + text;
            statusEl.style.color = color;
            flashElement(statusEl);
        } else if (!statusEl) {
            console.warn('[VM-Live] ⚠️  Status element not found for VM', data.vm_id);
        }
        
        // Update tools status
        const toolsEl = document.getElementById('vm-tools-' + data.vm_id);
        if (toolsEl && data.tools_status !== undefined && data.tools_status !== null) {
            const hasOk = (data.tools_status || '').toLowerCase().includes('ok');
            const color = hasOk ? '#10b981' : '#ef4444';
            console.log('[VM-Live] Updating tools for VM', data.vm_id, ':', data.tools_status);
            toolsEl.textContent = data.tools_status || 'Unknown';
            toolsEl.style.color = color;
            flashElement(toolsEl);
        } else if (!toolsEl) {
            console.warn('[VM-Live] ⚠️  Tools element not found for VM', data.vm_id);
        }
        
        // Update CPU usage
        const cpuEl = document.getElementById('vm-cpu-' + data.vm_id);
        if (cpuEl) {
            console.log('[VM-Live] Updating CPU for VM', data.vm_id, ':', data.cpu_usage_mhz, 'MHz');
            cpuEl.textContent = (data.cpu_usage_mhz || 0) + ' MHz';
            flashElement(cpuEl);
        }
        
        // Update memory usage
        const memEl = document.getElementById('vm-memory-' + data.vm_id);
        if (memEl) {
            console.log('[VM-Live] Updating memory for VM', data.vm_id, ':', data.memory_mb, 'MB');
            memEl.textContent = (data.memory_mb || 0) + ' MB';
            flashElement(memEl);
        }
        
        // Update storage (if available)
        if (data.storage_used_gb !== undefined) {
            const storEl = document.getElementById('vm-storage-' + data.vm_id);
            if (storEl) {
                console.log('[VM-Live] Updating storage for VM', data.vm_id, ':', data.storage_used_gb, 'GB');
                storEl.textContent = (data.storage_used_gb || 0) + ' GB';
                flashElement(storEl);
            }
        }
        
        // Update storage used (detail view)
        const storUsedEl = document.getElementById('vm-storage-used-' + data.vm_id);
        if (storUsedEl && data.storage_used_gb !== undefined) {
            const usedGb = parseFloat(data.storage_used_gb || 0).toFixed(2);
            console.log('[VM-Live] Updating storage used for VM', data.vm_id, ':', usedGb, 'GB');
            storUsedEl.textContent = usedGb + ' GB';
            flashElement(storUsedEl);
        }
        
        // Update storage provisioned (detail view)
        const storProvEl = document.getElementById('vm-storage-prov-' + data.vm_id);
        if (storProvEl && data.storage_provisioned_gb !== undefined) {
            const provGb = parseFloat(data.storage_provisioned_gb || 0).toFixed(2);
            console.log('[VM-Live] Updating storage provisioned for VM', data.vm_id, ':', provGb, 'GB');
            storProvEl.textContent = provGb + ' GB';
            flashElement(storProvEl);
        }
    }
    
    function flashElement(el) {
        return el;
    }
    
    function updateHostUI(data) {
        // Update host CPU usage %
        const cpuEl = document.getElementById('host-cpu-' + data.host_id);
        if (cpuEl) {
            const cpuPercent = (data.cpu_usage_percent || 0).toFixed(1);
            console.log('[VM-Live] Updating host CPU for host', data.host_id, ':', cpuPercent, '%');
            cpuEl.textContent = cpuPercent + '%';
            flashElement(cpuEl);
        } else {
            console.warn('[VM-Live] ⚠️  Host CPU element not found for Host', data.host_id);
        }
        
        // Update host memory usage %
        const memEl = document.getElementById('host-mem-' + data.host_id);
        if (memEl) {
            const memPercent = (data.memory_usage_percent || 0).toFixed(1);
            console.log('[VM-Live] Updating host memory for host', data.host_id, ':', memPercent, '%');
            memEl.textContent = memPercent + '%';
            flashElement(memEl);
        } else {
            console.warn('[VM-Live] ⚠️  Host memory element not found for Host', data.host_id);
        }
    }
    
    // Start on page load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            console.log('[VM-Live] Starting on DOMContentLoaded');
            connect();
        });
    } else {
        console.log('[VM-Live] Starting immediately');
        connect();
    }
})();
