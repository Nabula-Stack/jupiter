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
            console.log('[VM-Live] Message received:', data.type, data.vm_name);
            
            if (data.type === 'vm_update') {
                console.log('[VM-Live] 🔄 Updating VM:', data.vm_name);
                updateUI(data);
            } else if (data.type === 'pong') {
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
        if (statusEl) {
            const isOn = data.power_state.toLowerCase().includes('on');
            const color = isOn ? '#10b981' : '#ef4444';
            const text = isOn ? 'Running' : 'Stopped';
            console.log('[VM-Live] Updating status for VM', data.vm_id, ':', text);
            statusEl.textContent = '● ' + text;
            statusEl.style.color = color;
            flashElement(statusEl);
        } else {
            console.warn('[VM-Live] ⚠️  Status element not found for VM', data.vm_id);
        }
        
        // Update tools status
        const toolsEl = document.getElementById('vm-tools-' + data.vm_id);
        if (toolsEl) {
            const hasOk = (data.tools_status || '').toLowerCase().includes('ok');
            const color = hasOk ? '#10b981' : '#ef4444';
            console.log('[VM-Live] Updating tools for VM', data.vm_id, ':', data.tools_status);
            toolsEl.textContent = data.tools_status || 'Unknown';
            toolsEl.style.color = color;
            flashElement(toolsEl);
        } else {
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
    }
    
    function flashElement(el) {
        const orig = el.style.backgroundColor;
        el.style.backgroundColor = '#ffffcc';
        setTimeout(function() {
            el.style.backgroundColor = orig || '';
        }, 300);
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
