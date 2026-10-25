from __future__ import annotations

"""Modul Helper Unggah FTP/SFTP."""
import ftplib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

def upload_to_ftp(conn_info: Any, local_file_path: Path, remote_dir: str = "") -> str:
    if not local_file_path.exists():
        raise FileNotFoundError(f"File lokal tidak ditemukan: {local_file_path}")
    
    filename = local_file_path.name
    target_dir = remote_dir or conn_info.database or "/"
    
    with ftplib.FTP() as ftp:
        ftp.connect(conn_info.host, int(conn_info.port or 21), timeout=30)
        ftp.login(conn_info.username, getattr(conn_info, "password", ""))
        
        if target_dir and target_dir != "/":
            try:
                ftp.cwd(target_dir)
            except ftplib.error_perm:
                ftp.mkd(target_dir)
                ftp.cwd(target_dir)
                
        with open(local_file_path, "rb") as f:
            ftp.storbinary(f"STOR {filename}", f)
            
    return f"ftp://{conn_info.host}:{conn_info.port}/{target_dir.strip('/')}/{filename}"

def upload_to_sftp(conn_info: Any, local_file_path: Path, remote_dir: str = "") -> str:
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("Pustaka 'paramiko' belum terinstal. Jalankan: pip install paramiko")
        
    if not local_file_path.exists():
        raise FileNotFoundError(f"File lokal tidak ditemukan: {local_file_path}")
        
    filename = local_file_path.name
    target_dir = remote_dir or conn_info.database or "."
    
    transport = paramiko.Transport((conn_info.host, int(conn_info.port or 22)))
    transport.connect(username=conn_info.username, password=getattr(conn_info, "password", ""))
    
    with paramiko.SFTPClient.from_transport(transport) as sftp:
        if target_dir and target_dir != ".":
            try:
                sftp.chdir(target_dir)
            except IOError:
                sftp.mkdir(target_dir)
                sftp.chdir(target_dir)
                
        remote_path = f"{target_dir.rstrip('/')}/{filename}"
        sftp.put(str(local_file_path), remote_path)
        
    transport.close()
    return f"sftp://{conn_info.host}:{conn_info.port}/{remote_path.lstrip('/')}"

def execute_ftp_node(conn_info: Any, local_file_path: Path, config: Dict[str, Any]) -> str:
    remote_dir = config.get("remote_dir", "")
    if conn_info.db_type == "sftp":
        return upload_to_sftp(conn_info, local_file_path, remote_dir)
    return upload_to_ftp(conn_info, local_file_path, remote_dir)
