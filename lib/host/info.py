# lib/host/info.py
import re

def get_host_summary(conn):
    """Returns version and model info."""
    version = conn.run("vmware -v")
    # Targets the Product Name specifically
    model = conn.run("esxcli hardware platform get | grep 'Product Name' | cut -d ':' -f2")
    
    # Clean up model - remove extra whitespace, return empty string if just whitespace
    model_clean = model.strip() if model else ""
    if not model_clean:
        model_clean = "Unknown Model"
    
    return {
        "version": version.strip() if version else "Unknown",
        "model": model_clean
    }

def get_host_hardware(conn):
    """Returns total RAM in GB and CPU count."""
    # 1. Get RAM
    raw_mem = conn.run("smbiosDump | grep 'Size:' | grep 'MB'")
    ram_display = "Unknown"
    
    try:
        total_mb = 0
        if raw_mem:
            lines = raw_mem.strip().split('\n')
            for line in lines:
                if 'MB' in line:
                    # Extracts digits from "Size: 8192 MB"
                    mb_value = re.findall(r'\d+', line)
                    if mb_value:
                        total_mb += int(mb_value[0])
        
        if total_mb > 0:
            ram_gb = round(total_mb / 1024, 2)
            ram_display = ram_gb # Returning as float/int for DB compatibility
        else:
            # Fallback to vim-cmd if smbiosDump fails
            fallback = conn.run("vim-cmd hostsvc/hostsummary | grep 'memorySize'")
            bytes_val = int(re.search(r'\d+', fallback).group())
            ram_display = round(bytes_val / (1024**3), 2)
            
    except Exception:
        ram_display = 0

    # 2. Get CPU Count
    cpu_raw = conn.run("esxcli hardware cpu list | grep 'ID:' | wc -l")
    try:
        cpu_count = int(cpu_raw.strip()) if cpu_raw else 0
    except:
        cpu_count = 0

    return {
        "memory_total_gb": ram_display,
        "cpu_count": cpu_count,
        "vendor": "VMware/Intel",
        "status": "Online"
    }

def get_license_details(conn):
    """Parses license and cleans up the CPU/Product string."""
    raw = conn.run("vim-cmd hostsvc/hostsummary")
    
    # Get CPU brand name - try multiple approaches
    processor = "Unknown Processor"
    
    try:
        # Approach 1: esxcfg-info
        cpu_brand = conn.run("esxcfg-info -u 2>/dev/null | grep -i 'brand name' | head -1")
        if cpu_brand and cpu_brand.strip():
            # Extract everything after the colon
            if ':' in cpu_brand:
                processor = cpu_brand.split(':', 1)[1].strip()
            else:
                processor = cpu_brand.strip()
    except:
        pass
    
    # Approach 2: /proc/cpuinfo as fallback
    if processor == "Unknown Processor":
        try:
            cpu_model = conn.run("cat /proc/cpuinfo | grep 'model name' | head -1")
            if cpu_model and cpu_model.strip():
                if 'model name' in cpu_model:
                    processor = cpu_model.split(':', 1)[1].strip()
                else:
                    processor = cpu_model.strip()
        except:
            pass
    
    # Approach 3: lscpu as fallback
    if processor == "Unknown Processor":
        try:
            lscpu_out = conn.run("lscpu 2>/dev/null | grep 'Model name' | head -1")
            if lscpu_out and lscpu_out.strip():
                if ':' in lscpu_out:
                    processor = lscpu_out.split(':', 1)[1].strip()
                else:
                    processor = lscpu_out.strip()
        except:
            pass

    data = {
        "product": processor,
        "key": "See ESXi Portal",
        "status": "Evaluation/Licensed"
    }

    try:
        # Get the vSphere License Name
        name_match = re.search(r'name\s*=\s*"(.*)"', raw)
        if name_match:
            data["status"] = name_match.group(1).strip()
    except:
        pass
        
    return data

def get_host_usage_stats(conn):
    """Parses live CPU and RAM usage percentages."""
    raw = conn.run("vim-cmd hostsvc/hostsummary")
    
    stats = {
        "cpu_usage_percent": 0,
        "memory_usage_percent": 0
    }
    
    try:
        # Parse CPU MHz used
        cpu_used_match = re.search(r'overallCpuUsage\s*=\s*(\d+)', raw)
        if cpu_used_match:
            # We estimate 100% as roughly 20000MHz for a standard lab host 
            # or you can calculate it against total Hz if available.
            cpu_mhz = int(cpu_used_match.group(1))
            stats["cpu_usage_percent"] = min(99, int((cpu_mhz / 20000) * 100))
        
        # Parse RAM MB used
        mem_used_match = re.search(r'overallMemoryUsage\s*=\s*(\d+)', raw)
        if mem_used_match:
            mem_mb = int(mem_used_match.group(1))
            # Assuming 32GB host (32768MB)
            stats["memory_usage_percent"] = round((mem_mb / 32768) * 100)
    except:
        pass
        
    return stats

def get_host_runtime(conn):
    """Returns the uptime string."""
    uptime = conn.run("uptime")
    return uptime.strip() if uptime else "Unknown"