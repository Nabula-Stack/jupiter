import re

def get_vm_details(host, vmid):
    """
    Parses 'vim-cmd vmsvc/get.summary' and 'get.guest' for deeper guest info.
    Updated to reliably parse nested ipStack for DNS and deduplicate IPs.
    """
    summary = host.run(f"vim-cmd vmsvc/get.summary {vmid}")
    guest = host.run(f"vim-cmd vmsvc/get.guest {vmid}")
    
    def find(pattern, text, default="N/A"):
        m = re.search(pattern, text)
        return m.group(1) if m else default

    # 1. Parse Basic Info
    power_state = "poweredOff"
    if "poweredOn" in summary: power_state = "poweredOn"
    elif "suspended" in summary: power_state = "suspended"
    
    # DEBUG: Log what we're parsing
    import sys
    print(f"[DEBUG] VM {vmid}: power_state={power_state}", file=sys.stderr)

    # 2. Parse Network Interfaces (NIC Info)
    networks = []
    net_block_match = re.search(r'net\s*=\s*\(vim\.vm\.GuestInfo\.NicInfo\)\s*\[(.*?)\]\s*,\s*ipStack', guest, re.DOTALL)
    if net_block_match:
        net_content = net_block_match.group(1)
        nic_chunks = re.split(r'\(vim\.vm\.GuestInfo\.NicInfo\)', net_content)
        for chunk in nic_chunks:
            if "network =" not in chunk: continue
            
            net_name = find(r'network\s*=\s*"([^"]+)"', chunk)
            mac_addr = find(r'macAddress\s*=\s*"([^"]+)"', chunk)
            
            ips = []
            ip_list_match = re.search(r'ipAddress\s*=\s*\(string\)\s*\[(.*?)\]', chunk, re.DOTALL)
            if ip_list_match:
                # Get IPs and deduplicate
                raw_ips = re.findall(r'"([\d.]+)"', ip_list_match.group(1))
                ips = list(dict.fromkeys(raw_ips))
            
            networks.append({
                "network": net_name,
                "mac": mac_addr,
                "ip": ips
            })

    # 3. Parse DNS (Specifically targeting the ipStack -> dnsConfig block)
    dns_servers = []
    dns_section = re.search(r'dnsConfig\s*=\s*\(vim\.net\.DnsConfigInfo\)\s*\{(.*?)\}\s*,', guest, re.DOTALL)
    if dns_section:
        dns_ips_match = re.search(r'ipAddress\s*=\s*\(string\)\s*\[(.*?)\]', dns_section.group(1), re.DOTALL)
        if dns_ips_match:
            raw_dns = re.findall(r'"([\d.]+)"', dns_ips_match.group(1))
            dns_servers = list(dict.fromkeys(raw_dns))

    # 4. Storage Calculation
    comm = find(r'committed\s*=\s*(\d+)', summary, "0")
    uncomm = find(r'uncommitted\s*=\s*(\d+)', summary, "0")
    
    # 5. OS Details
    detailed = find(r'guestDetailedData\s*=\s*"([^"]+)"', guest)

    # 6. Runtime extraction (to include in full details)
    runtime = get_vm_runtime_stats(host, vmid)

    return {
        "vmid": vmid,
        "vm_name": find(r'name\s*=\s*"([^"]+)"', summary),
        "power_state": power_state,
        "guest_name": find(r'guestFullName\s*=\s*"([^"]+)"', summary),
        "ip_address": find(r'ipAddress\s*=\s*"([\d.]+)"', summary),
        "tools_status": find(r'toolsStatus\s*=\s*"([^"]+)"', summary),
        "tools_running": find(r'toolsRunningStatus\s*=\s*"([^"]+)"', guest),
        "distro": find(r"prettyName='([^']+)'", detailed),
        "kernel": find(r"kernelVersion='([^']+)'", detailed),
        "hw_version": find(r'hwVersion\s*=\s*"([^"]+)"', summary),
        "uuid": find(r'uuid\s*=\s*"([^"]+)"', summary),
        "num_cpu": find(r'numCpu\s*=\s*(\d+)', summary),
        "memory_mb": find(r'memorySizeMB\s*=\s*(\d+)', summary),
        "storage_used_gb": round(int(comm) / (1024**3), 2),
        "storage_provisioned_gb": round((int(comm) + int(uncomm)) / (1024**3), 2),
        "overall_status": find(r'overallStatus\s*=\s*"([^"]+)"', summary),
        "networks": networks,     
        "dns_servers": dns_servers,
        **runtime  # Merges CPU/Mem/Uptime into the main dictionary
    }

def get_vm_runtime_stats(host, vmid):
    """
    Parses 'vim-cmd vmsvc/get.summary' for real-time CPU/RAM usage and uptime.
    """
    summary = host.run(f"vim-cmd vmsvc/get.summary {vmid}")
    
    cpu = re.search(r'overallCpuUsage\s*=\s*(\d+)', summary)
    mem = re.search(r'guestMemoryUsage\s*=\s*(\d+)', summary)
    uptime = re.search(r'uptimeSeconds\s*=\s*(\d+)', summary)
    
    u_sec = int(uptime.group(1)) if uptime else 0
    return {
        "cpu_usage_mhz": cpu.group(1) if cpu else "0",
        "memory_usage_mb": mem.group(1) if mem else "0",
        "uptime_sec": str(u_sec),
        "uptime_human": f"{u_sec // 86400}d {(u_sec % 86400) // 3600}h" if u_sec > 0 else "0s"
    }

def get_vm_network_info(host, vmid):
    """
    Standalone NIC info extractor. Returns list of network cards with IPs.
    """
    guest_data = host.run(f"vim-cmd vmsvc/get.guest {vmid}")
    results = []

    # Parse individual NIC chunks from the guest info
    nic_blocks = re.findall(r'\(vim\.vm\.GuestInfo\.NicInfo\)\s*\{(.*?)\}', guest_data, re.DOTALL)

    for block in nic_blocks:
        network = re.search(r'network\s*=\s*"([^"]+)"', block)
        mac = re.search(r'macAddress\s*=\s*"([0-9a-fA-F:]+)"', block)
        
        # Extract IPs inside the (string) [] block for this specific NIC
        ips = []
        ip_list_match = re.search(r'ipAddress\s*=\s*\(string\)\s*\[(.*?)\]', block, re.DOTALL)
        if ip_list_match:
            found_ips = re.findall(r'"([\d.]+)"', ip_list_match.group(1))
            ips = list(dict.fromkeys(found_ips))

        if network or mac or ips:
            results.append({
                "network": network.group(1) if network else "Unknown",
                "mac": mac.group(1) if mac else "N/A",
                "ip": ips if ips else ["N/A"]
            })

    # Fallback to Config Devices if Guest Tools aren't reporting (Offline status)
    if not results:
        config_data = host.run(f"vim-cmd vmsvc/get.config {vmid}")
        dev_blocks = re.findall(r'\(vim\.vm\.device\.VirtualEthernetCard\)\s*\{(.*?)\}', config_data, re.DOTALL)

        for dev in dev_blocks:
            network = re.search(r'networkName\s*=\s*"([^"]+)"', dev)
            mac = re.search(r'macAddress\s*=\s*"([0-9a-fA-F:]+)"', dev)
            if network or mac:
                results.append({
                    "network": network.group(1) if network else "Unknown",
                    "mac": mac.group(1) if mac else "N/A",
                    "ip": ["N/A"]
                })

    return results if results else [{"network": "Disconnected", "mac": "N/A", "ip": ["N/A"]}]