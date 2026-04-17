/**
 * Django Channels WebSocket Client for Real-Time VM Updates
 * Compatible with Django Unfold Admin Interface
 * 
 * Usage:
 *   <script src="{% static 'js/vm_realtime_updates.js' %}"></script>
 */

(function() {
    'use strict';
    
    // Configuration
    const wsConfig = {
        protocol: window.location.protocol === 'https:' ? 'wss:' : 'ws:',
        host: window.location.host,
        path: '/ws/vms/updates/',
        reconnectDelay: 3000,
        maxReconnectAttempts: 5,
        pingInterval: 30000,
    };
    
    // State
    let socket = null;
    let reconnectAttempts = 0;
    let pingIntervalId = null;
    
    // Get WebSocket URL
    function getWsUrl() {
        return `${wsConfig.protocol}//${wsConfig.host}${wsConfig.path}`;
    }
    
    // Connect to WebSocket
    function connect() {
        // Only connect if on Unfold admin change_list page
        if (!isUnfoldAdminPage()) {
            console.log('[VM-WebSocket] Not on Unfold admin page. WebSocket disabled.');
            return;
        }
        
        const wsUrl = getWsUrl();
        console.log('[VM-WebSocket] Connecting to:', wsUrl);
        
        try {
            socket = new WebSocket(wsUrl);
            
            socket.onopen = handleOpen;
            socket.onmessage = handleMessage;
            socket.onerror = handleError;
            socket.onclose = handleClose;
        } catch (error) {
            console.error('[VM-WebSocket] Connection error:', error);
            scheduleReconnect();
        }
    }
    
    // Check if we're on Unfold admin change_list page
    function isUnfoldAdminPage() {
        return document.body.classList.contains('change-list') || 
               window.location.pathname.includes('/admin/');
    }
    
    // WebSocket Event Handlers
    function handleOpen(event) {
        console.log('[VM-WebSocket] Connected successfully');
        reconnectAttempts = 0;
        
        // Send initial ping
        send({ type: 'ping' });
        
        // Set up periodic ping
        setupPingInterval();
        
        // Dispatch custom event
        dispatchEvent('vm-websocket-open');
        
        // Show connection indicator
        showConnectionStatus(true);
    }
    
    function handleMessage(event) {
        try {
            const data = JSON.parse(event.data);
            
            if (data.type === 'vm_update') {
                console.log('[VM-WebSocket] Update received:', data);
                updateVMInUnfold(data);
                dispatchEvent('vm-update', { detail: data });
            } else if (data.type === 'pong') {
                console.log('[VM-WebSocket] Pong - connection alive');
            }
        } catch (error) {
            console.error('[VM-WebSocket] Message parse error:', error);
        }
    }
    
    function handleError(error) {
        console.error('[VM-WebSocket] Error:', error);
        dispatchEvent('vm-websocket-error', { detail: error });
    }
    
    function handleClose(event) {
        console.log('[VM-WebSocket] Closed. Code:', event.code, 'Reason:', event.reason);
        cleanupPingInterval();
        dispatchEvent('vm-websocket-close');
        showConnectionStatus(false);
        scheduleReconnect();
    }
    
    // Send message to WebSocket
    function send(message) {
        if (socket && socket.readyState === WebSocket.OPEN) {
            try {
                socket.send(JSON.stringify(message));
            } catch (error) {
                console.error('[VM-WebSocket] Send error:', error);
            }
        }
    }
    
    // Handle Reconnection
    function scheduleReconnect() {
        if (reconnectAttempts < wsConfig.maxReconnectAttempts) {
            reconnectAttempts++;
            const delay = wsConfig.reconnectDelay * reconnectAttempts;
            console.log(`[VM-WebSocket] Reconnecting in ${delay}ms (attempt ${reconnectAttempts}/${wsConfig.maxReconnectAttempts})`);
            setTimeout(connect, delay);
        } else {
            console.error('[VM-WebSocket] Max reconnection attempts reached');
            dispatchEvent('vm-websocket-failed');
        }
    }
    
    // Ping Management
    function setupPingInterval() {
        cleanupPingInterval();
        pingIntervalId = setInterval(function() {
            if (socket && socket.readyState === WebSocket.OPEN) {
                send({ type: 'ping' });
            }
        }, wsConfig.pingInterval);
    }
    
    function cleanupPingInterval() {
        if (pingIntervalId) {
            clearInterval(pingIntervalId);
            pingIntervalId = null;
        }
    }
    
    // Update VM in Unfold Admin Table
    function updateVMInUnfold(data) {
        // Find the row by data-vm-id attribute
        const row = document.querySelector(`tr [data-vm-id="${data.vm_id}"]`)?.closest('tr');
        if (!row) {
            console.log(`[VM-WebSocket] Row for VM ${data.vm_id} not found`);
            return;
        }
        
        // Update Power State Status (usually in the first cell after checkbox)
        const powerStateElement = row.querySelector('.vm-power-state');
        if (powerStateElement) {
            const s = (data.power_state || "").toLowerCase();
            const isOn = s.includes("on");
            const isSuspended = s.includes("suspend");
            const color = isOn ? "#10b981" : (isSuspended ? "#f59e0b" : "#ef4444");
            const text = isOn ? "Running" : (isSuspended ? "Suspended" : "Stopped");
            
            powerStateElement.textContent = `● ${text}`;
            powerStateElement.style.color = color;
            powerStateElement.className = `vm-power-state status-${s}`;
            console.log(`[VM-WebSocket] Updated power state for VM ${data.vm_name}: ${data.power_state}`);
        }
        
        // Update Tools Status
        const toolsElement = row.querySelector('.vm-tools-status');
        if (toolsElement) {
            toolsElement.textContent = data.tools_status || "Unknown";
            const hasOk = (data.tools_status || "").toLowerCase().includes('ok');
            const color = hasOk ? "#10b981" : "#ef4444";
            toolsElement.style.color = color;
            console.log(`[VM-WebSocket] Updated tools status for VM ${data.vm_name}: ${data.tools_status}`);
        }
        
        // Flash animation to indicate update
        flashRow(row);
    }
    
    // Flash animation on table row update
    function flashRow(row) {
        return row;
    }
    
    // Show connection status indicator
    function showConnectionStatus(connected) {
        let statusEl = document.getElementById('vm-websocket-status');
        
        if (!statusEl) {
            // Create status indicator if it doesn't exist
            statusEl = document.createElement('div');
            statusEl.id = 'vm-websocket-status';
            statusEl.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                padding: 10px 16px;
                border-radius: 6px;
                font-size: 14px;
                font-weight: 500;
                z-index: 9999;
                transition: opacity 0.3s ease;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            `;
            document.body.appendChild(statusEl);
        }
        
        if (connected) {
            statusEl.style.backgroundColor = '#d4edda';
            statusEl.style.color = '#155724';
            statusEl.textContent = '🟢 WebSocket Live';
            statusEl.style.opacity = '1';
        } else {
            statusEl.style.backgroundColor = '#f8d7da';
            statusEl.style.color = '#721c24';
            statusEl.textContent = '🔴 WebSocket Offline';
            statusEl.style.opacity = '0.9';
        }
    }
    
    // Dispatch custom DOM events
    function dispatchEvent(eventName, options = {}) {
        const event = new CustomEvent(eventName, options);
        document.dispatchEvent(event);
    }
    
    // Public API (if needed)
    window.VMWebSocket = {
        connect: connect,
        disconnect: () => {
            cleanupPingInterval();
            if (socket) socket.close();
        },
        send: send,
        isConnected: () => socket && socket.readyState === WebSocket.OPEN,
        getSocket: () => socket,
        onChange: (callback) => {
            document.addEventListener('vm-update', (e) => callback(e.detail));
        },
    };
    
    // Initialize on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', connect);
    } else {
        connect();
    }
})();
