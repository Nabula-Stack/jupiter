/**
 * vCenter API Client Library
 * Unified interface for API calls with integrated WebSocket event handling
 * 
 * Usage:
 *   const client = new VCenterAPIClient('/api/v1');
 *   
 *   // Listen for events
 *   client.on('vm_power_state_changed', (data) => {
 *       console.log('VM power changed:', data.vm_name, data.power_state);
 *   });
 *   
 *   // Make API calls
 *   await client.vms.powerOn('host1', 'vm-123');
 */

class VCenterAPIClient {
    constructor(apiBaseUrl = '/api/v1', wsBaseUrl = '') {
        this.apiBaseUrl = apiBaseUrl;
        this.wsBaseUrl = wsBaseUrl || this.getWebSocketUrl();
        this.ws = null;
        this.eventListeners = {};
        this.isConnected = false;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 3000;
        
        // Initialize WebSocket immediately
        this.connectWebSocket();
        
        // Create namespaced API methods
        this.vms = new VMApi(this);
        this.hosts = new HostApi(this);
        this.network = new NetworkApi(this);
        this.storage = new StorageApi(this);
    }
    
    /**
     * Get WebSocket URL from current location
     */
    getWebSocketUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        const path = '/ws/vms/updates/';
        return `${protocol}//${host}${path}`;
    }
    
    /**
     * Connect to WebSocket server
     */
    connectWebSocket() {
        try {
            console.log('[API] Connecting to WebSocket:', this.wsBaseUrl);
            this.ws = new WebSocket(this.wsBaseUrl);
            
            this.ws.onopen = () => this.handleWebSocketOpen();
            this.ws.onmessage = (event) => this.handleWebSocketMessage(event);
            this.ws.onerror = (error) => this.handleWebSocketError(error);
            this.ws.onclose = () => this.handleWebSocketClose();
        } catch (error) {
            console.error('[API] WebSocket connection error:', error);
            this.scheduleReconnect();
        }
    }
    
    /**
     * Handle WebSocket open event
     */
    handleWebSocketOpen() {
        console.log('[API] ✅ WebSocket connected');
        this.isConnected = true;
        this.reconnectAttempts = 0;
        
        // Send initial ping
        this.sendWebSocketMessage({ type: 'ping' });
        
        // Setup periodic ping
        setInterval(() => {
            if (this.isConnected && this.ws.readyState === WebSocket.OPEN) {
                this.sendWebSocketMessage({ type: 'ping' });
            }
        }, 30000);
        
        // Emit connection event
        this.emit('connected');
    }
    
    /**
     * Handle WebSocket message
     */
    handleWebSocketMessage(event) {
        try {
            const data = JSON.parse(event.data);
            
            if (data.type === 'pong') {
                // Ignore pong responses
                return;
            }
            
            console.log('[API] Received event:', data.event_type || data.type);
            
            // Get the event type (could be 'event_type' or 'type' field)
            const eventType = data.event_type || data.type;
            
            // Emit event with type
            this.emit(eventType, data);
            
            // Also emit a generic 'update' event with the full data
            this.emit('update', data);
        } catch (error) {
            console.error('[API] Message parse error:', error);
        }
    }
    
    /**
     * Handle WebSocket error
     */
    handleWebSocketError(error) {
        console.error('[API] ❌ WebSocket error:', error);
        this.emit('error', error);
    }
    
    /**
     * Handle WebSocket close
     */
    handleWebSocketClose() {
        console.warn('[API] ⚠️  WebSocket disconnected');
        this.isConnected = false;
        this.emit('disconnected');
        this.scheduleReconnect();
    }
    
    /**
     * Schedule WebSocket reconnection
     */
    scheduleReconnect() {
        if (this.reconnectAttempts < this.maxReconnectAttempts) {
            this.reconnectAttempts++;
            const delay = this.reconnectDelay * this.reconnectAttempts;
            console.log(`[API] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
            setTimeout(() => this.connectWebSocket(), delay);
        }
    }
    
    /**
     * Send message via WebSocket
     */
    sendWebSocketMessage(message) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(JSON.stringify(message));
            } catch (error) {
                console.error('[API] Failed to send WebSocket message:', error);
            }
        }
    }
    
    /**
     * Register event listener
     */
    on(eventType, callback) {
        if (!this.eventListeners[eventType]) {
            this.eventListeners[eventType] = [];
        }
        this.eventListeners[eventType].push(callback);
    }
    
    /**
     * Unregister event listener
     */
    off(eventType, callback) {
        if (!this.eventListeners[eventType]) return;
        this.eventListeners[eventType] = this.eventListeners[eventType].filter(
            cb => cb !== callback
        );
    }
    
    /**
     * Emit event to all listeners
     */
    emit(eventType, data = null) {
        if (!this.eventListeners[eventType]) return;
        this.eventListeners[eventType].forEach(callback => {
            try {
                callback(data);
            } catch (error) {
                console.error(`[API] Error in event listener for ${eventType}:`, error);
            }
        });
    }
    
    /**
     * Make HTTP request to API
     */
    async request(method, path, body = null) {
        const url = `${this.apiBaseUrl}${path}`;
        const options = {
            method,
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            }
        };
        
        if (body && (method === 'POST' || method === 'PUT' || method === 'PATCH')) {
            options.body = JSON.stringify(body);
        }
        
        try {
            const response = await fetch(url, options);
            
            if (!response.ok) {
                throw new Error(`API Error: ${response.status} ${response.statusText}`);
            }
            
            return await response.json();
        } catch (error) {
            console.error('[API] Request error:', error);
            throw error;
        }
    }
}

/**
 * VM Operations API
 */
class VMApi {
    constructor(client) {
        this.client = client;
    }
    
    /**
     * List all VMs for a host
     */
    async list(hostName) {
        return this.client.request('GET', `/vms/${hostName}/db/list`);
    }
    
    /**
     * Get VM details
     */
    async getDetails(hostName, vmId) {
        return this.client.request('GET', `/vms/${hostName}/${vmId}/details`);
    }
    
    /**
     * Power on VM
     */
    async powerOn(hostName, vmId) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/power`, {
            action: 'poweron'
        });
    }
    
    /**
     * Power off VM
     */
    async powerOff(hostName, vmId) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/power`, {
            action: 'poweroff'
        });
    }
    
    /**
     * Reboot VM
     */
    async reboot(hostName, vmId) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/power`, {
            action: 'reboot'
        });
    }
    
    /**
     * Shutdown VM
     */
    async shutdown(hostName, vmId) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/power`, {
            action: 'shutdown'
        });
    }
    
    /**
     * Reset VM
     */
    async reset(hostName, vmId) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/power`, {
            action: 'reset'
        });
    }
    
    /**
     * Suspend VM
     */
    async suspend(hostName, vmId) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/power`, {
            action: 'suspend'
        });
    }
    
    /**
     * Power operation
     */
    async power(hostName, vmId, action) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/power`, {
            action
        });
    }
    
    /**
     * Create snapshot
     */
    async createSnapshot(hostName, vmId, name = 'Auto-Snapshot') {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/snapshots`, {
            op: 'create',
            name
        });
    }
    
    /**
     * Restore snapshot
     */
    async restoreSnapshot(hostName, vmId, name) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/snapshots`, {
            op: 'restore',
            name
        });
    }
    
    /**
     * Delete snapshot
     */
    async deleteSnapshot(hostName, vmId, name) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/snapshots`, {
            op: 'delete',
            name
        });
    }

    /**
     * Delete (destroy) VM — permanently powers off, deletes all disk files, removes from inventory.
     * This action is irreversible.
     */
    async deleteVm(hostName, vmId) {
        return this.client.request('DELETE', `/vms/${hostName}/${vmId}/delete`);
    }

    /**
     * Create new VM
     */
    async create(hostName, {
        name,
        datastore,
        cpu = 2,
        ram = 2048,
        disk_size_gb = 16,
        disk_type = 'thin',
        guest_os = 'other-64',
        network_name = 'VM Network',
        nic_type = 'e1000',
        scsi_controller = 'lsilogic',
        firmware = 'bios',
        hw_version = '13',
        power_on = false
    }) {
        return this.client.request('POST', `/vms/${hostName}/create`, {
            name,
            datastore,
            cpu,
            ram,
            disk_size_gb,
            disk_type,
            guest_os,
            network_name,
            nic_type,
            scsi_controller,
            firmware,
            hw_version,
            power_on
        });
    }

    /**
     * Get VM create options for a host
     */
    async getCreateOptions(hostName) {
        return this.client.request('GET', `/vms/${hostName}/create/options`);
    }
    
    /**
     * Modify VM hardware
     */
    async modify(hostName, vmId, modification, options = {}) {
        return this.client.request('POST', `/vms/${hostName}/${vmId}/modify`, {
            modification,
            ...options
        });
    }
    
    /**
     * Get console redirect URL
     */
    getConsoleUrl(hostName, vmId) {
        return `${this.client.apiBaseUrl}/vms/${hostName}/${vmId}/console`;
    }
}

/**
 * Host Operations API
 */
class HostApi {
    constructor(client) {
        this.client = client;
    }
    
    /**
     * List all hosts
     */
    async list() {
        return this.client.request('GET', '/hosts/all');
    }
    
    /**
     * Get host summary
     */
    async getSummary(hostName) {
        return this.client.request('GET', `/hosts/${hostName}/summary`);
    }
    
    /**
     * Get host license info
     */
    async getLicense(hostName) {
        return this.client.request('GET', `/hosts/${hostName}/license`);
    }
    
    /**
     * Set license key
     */
    async setLicense(hostName, serialKey) {
        return this.client.request('POST', `/hosts/${hostName}/license`, {
            serial_key: serialKey
        });
    }
    
    /**
     * Reboot host
     */
    async reboot(hostName) {
        return this.client.request('POST', `/hosts/${hostName}/reboot`);
    }
    
    /**
     * Shutdown host
     */
    async shutdown(hostName) {
        return this.client.request('POST', `/hosts/${hostName}/shutdown`);
    }
}

/**
 * Network Operations API
 */
class NetworkApi {
    constructor(client) {
        this.client = client;
    }
    
    /**
     * Get network inventory
     */
    async getInventory(hostName) {
        return this.client.request('GET', `/network/${hostName}/inventory`);
    }
    
    /**
     * List vSwitches
     */
    async getSwitches(hostName) {
        return this.client.request('GET', `/network/${hostName}/vswitches`);
    }
    
    /**
     * List port groups
     */
    async getPortGroups(hostName) {
        return this.client.request('GET', `/network/${hostName}/portgroups`);
    }
    
    /**
     * Create port group
     */
    async createPortGroup(hostName, pgName, vswitch, vlan = 0) {
        return this.client.request('POST', `/network/${hostName}/portgroups`, {
            pg_name: pgName,
            vswitch,
            vlan
        });
    }
    
    /**
     * Delete port group
     */
    async deletePortGroup(hostName, pgName) {
        return this.client.request('DELETE', `/network/${hostName}/portgroups/${pgName}`);
    }
    
    /**
     * Create vSwitch
     */
    async createVSwitch(hostName, vswitchName) {
        return this.client.request('POST', `/network/${hostName}/vswitches`, {
            vswitch_name: vswitchName
        });
    }
    
    /**
     * Delete vSwitch
     */
    async deleteVSwitch(hostName, vswitchName) {
        return this.client.request('DELETE', `/network/${hostName}/vswitches/${vswitchName}`);
    }
}

/**
 * Storage Operations API
 */
class StorageApi {
    constructor(client) {
        this.client = client;
    }
    
    /**
     * List datastores
     */
    async getDatastores(hostName) {
        return this.client.request('GET', `/storage/${hostName}/datastores`);
    }
    
    /**
     * List physical disks
     */
    async getDisks(hostName) {
        return this.client.request('GET', `/storage/${hostName}/disks/available`);
    }
    
    /**
     * Browse directory
     */
    async browse(hostName, path = '/vmfs/volumes') {
        return this.client.request('GET', `/storage/${hostName}/explorer/ls?path=${encodeURIComponent(path)}`);
    }
    
    /**
     * Check if path exists
     */
    async pathExists(hostName, path) {
        return this.client.request('GET', `/storage/${hostName}/explorer/exists?path=${encodeURIComponent(path)}`);
    }
    
    /**
     * Create directory
     */
    async mkDir(hostName, path) {
        return this.client.request('POST', `/storage/${hostName}/explorer/mkdir`, {
            path
        });
    }
    
    /**
     * Delete file or directory
     */
    async delete(hostName, path) {
        return this.client.request('DELETE', `/storage/${hostName}/explorer/rm?path=${encodeURIComponent(path)}`);
    }
    
    /**
     * Initiate storage rescan
     */
    async rescan(hostName) {
        return this.client.request('POST', `/storage/${hostName}/rescan`);
    }
    
    /**
     * Create datastore
     */
    async createDatastore(hostName, diskId, name) {
        return this.client.request('POST', `/storage/${hostName}/datastore/create`, {
            disk_id: diskId,
            ds_name: name
        });
    }
}

/**
 * Export for use in both modules and browser
 */
if (typeof module !== 'undefined' && module.exports) {
    module.exports = VCenterAPIClient;
}
