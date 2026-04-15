/**
 * Host Admin - Conditional Field Visibility
 * 
 * Show/hide password and ssh_public_key fields based on selected hypervisor.
 * - ESXi: password for API mode, SSH key for SSH mode.
 * - KVM/libvirt: SSH key only.
 * - Other hypervisors: password only.
 */

document.addEventListener('DOMContentLoaded', function() {
    const hypervisorSelect = document.getElementById('id_hypervisor_type');
    const connectionMethodSelect = document.getElementById('id_esxi_connection_method');
    
    // Find fields by looking for input elements and their containers
    let passwordField = document.getElementById('id_password');
    let sshKeyField = document.getElementById('id_ssh_public_key');
    
    if (!hypervisorSelect || !connectionMethodSelect || !passwordField || !sshKeyField) {
        console.warn('Host admin fields not found');
        return; // Fields not found, exit gracefully
    }

    // Get the parent fieldrow/fieldbox containers
    passwordField = passwordField.closest('.field, .fieldrow, [class*="field"]') || passwordField.parentElement.parentElement;
    sshKeyField = sshKeyField.closest('.field, .fieldrow, [class*="field"]') || sshKeyField.parentElement.parentElement;

    function updateFieldVisibility() {
        const hypervisor = hypervisorSelect.value;
        const connectionMethod = connectionMethodSelect.value;
        
        console.log('updateFieldVisibility:', { hypervisor, connectionMethod });
        
        // ESXi supports both API password auth and SSH key auth.
        if (hypervisor === 'vmware_esxi') {
            // Show API-related fields only
            const connectionMethodParent = connectionMethodSelect.closest('.field, .fieldrow, [class*="field"]') || connectionMethodSelect.parentElement.parentElement;
            connectionMethodParent.style.display = 'block';
            
            if (connectionMethod === 'api') {
                // API mode: show password, hide SSH key
                passwordField.style.display = 'block';
                sshKeyField.style.display = 'none';
            } else {
                // SSH mode: hide password, show SSH key
                passwordField.style.display = 'none';
                sshKeyField.style.display = 'block';
            }
        } else if (hypervisor === 'kvm_libvirt') {
            // KVM/libvirt uses SSH key auth.
            const connectionMethodParent = connectionMethodSelect.closest('.field, .fieldrow, [class*="field"]') || connectionMethodSelect.parentElement.parentElement;
            connectionMethodParent.style.display = 'none';
            passwordField.style.display = 'none';
            sshKeyField.style.display = 'block';
        } else {
            // Non-ESXi/KVM hypervisors: hide connection method selector, show password.
            const connectionMethodParent = connectionMethodSelect.closest('.field, .fieldrow, [class*="field"]') || connectionMethodSelect.parentElement.parentElement;
            connectionMethodParent.style.display = 'none';
            passwordField.style.display = 'block';
            sshKeyField.style.display = 'none';
        }
    }

    // Initial state
    updateFieldVisibility();

    // Listen for changes
    hypervisorSelect.addEventListener('change', updateFieldVisibility);
    connectionMethodSelect.addEventListener('change', updateFieldVisibility);
});
