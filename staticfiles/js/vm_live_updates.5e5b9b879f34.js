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
        console.log('[VM-Live] Connected');
        reconnectAttempts = 0;
        send({type: 'ping'});
        
        // Keep connection alive
        setInterval(function() {
            if (socket && socket.readyState === 1) send({type: 'ping'});
        }, 30000);
    }
    
    function onMessage(event) {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'vm_update') {
                updateUI(data);
            }
        } catch(e) {
            console.error('[VM-Live] Parse error:', e);
        }
    }
    
    function onError(error) {
        console.error('[VM-Live] Error:', error);
    }
    
    function onClose() {
        console.log('[VM-Live] Disconnected - reconnecting...');
        if (reconnectAttempts < MAX_RECONNECT) {
            reconnectAttempts++;
            setTimeout(connect, RECONNECT_DELAY * reconnectAttempts);
        }
    }
    
    function send(msg) {
        if (socket && socket.readyState === 1) {
            try { socket.send(JSON.stringify(msg)); } catch(e) {}
        }
    }
    
    function updateUI(data) {
        // Update power status
        const statusEl = document.getElementById('vm-status-' + data.vm_id);
        if (statusEl) {
            const isOn = data.power_state.toLowerCase().includes('on');
            const color = isOn ? '#10b981' : '#ef4444';
            const text = isOn ? 'Running' : 'Stopped';
            statusEl.textContent = '● ' + text;
            statusEl.style.color = color;
            flashElement(statusEl);
        }
        
        // Update tools status
        const toolsEl = document.getElementById('vm-tools-' + data.vm_id);
        if (toolsEl) {
            const hasOk = (data.tools_status || '').toLowerCase().includes('ok');
            const color = hasOk ? '#10b981' : '#ef4444';
            toolsEl.textContent = data.tools_status || 'Unknown';
            toolsEl.style.color = color;
            flashElement(toolsEl);
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
        document.addEventListener('DOMContentLoaded', connect);
    } else {
        connect();
    }
})();
