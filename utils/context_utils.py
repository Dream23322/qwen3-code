"""
Utilities for condensing and managing context to improve LLM response speed
and relevance.
"""

import os
import re
from pathlib import Path
from typing import List, Set, Tuple

def get_ignored_paths(base_path: str) -> Set[str]:
    """Get set of ignored paths from .gitignore and common patterns."""
    ignored = set()
    
    # Load .gitignore if exists
    gitignore_path = Path(base_path) / ".gitignore"
    if gitignore_path.exists():
        try:
            with open(gitignore_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        ignored.add(line)
        except Exception:
            pass
    
    # Add common ignored patterns
    ignored.update([
        "node_modules", "__pycache__", ".venv", ".git", 
        "*.pyc", "*.log", "*.tmp", "dist", "build",
        ".pytest_cache", ".coverage", "coverage.xml"
    ])
    
    return ignored

def should_ignore_path(path: str, ignored_paths: Set[str]) -> bool:
    """Check if a path should be ignored based on ignored patterns."""
    path_obj = Path(path)
    
    # Check if any part of the path matches ignored patterns
    for ignored in ignored_paths:
        if ignored.endswith('/'):
            # Directory pattern
            if path_obj.is_dir() and path.startswith(ignored.rstrip('/')):
                return True
        elif '*' in ignored:
            # Glob pattern
            if re.match(f'^{re.escape(ignored).replace("\\*", ".*")}$', path):
                return True
        else:
            # Exact match
            if path == ignored or path.startswith(ignored + '/'):
                return True
    
    return False

def get_project_files(cwd: str, max_files: int = 100) -> List[Tuple[str, str]]:
    """
    Get list of files in project, excluding ignored paths.
    
    Returns list of (relative_path, content) tuples.
    """
    ignored = get_ignored_paths(cwd)
    files = []
    
    try:
        for root, dirs, filenames in os.walk(cwd):
            # Filter out ignored directories
            dirs[:] = [d for d in dirs if not should_ignore_path(os.path.join(root, d), ignored)]
            
            # Process files
            for filename in filenames:
                file_path = os.path.join(root, filename)
                
                # Skip ignored files
                if should_ignore_path(file_path, ignored):
                    continue
                
                # Get relative path
                rel_path = os.path.relpath(file_path, cwd)
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        files.append((rel_path, content))
                        
                        # Limit number of files to avoid overwhelming context
                        if len(files) >= max_files:
                            break
                except (UnicodeDecodeError, PermissionError):
                    # Skip files that can't be read
                    continue
            
            if len(files) >= max_files:
                break
                
    except Exception:
        # Fallback to simple file listing if walk fails
        pass
    
    return files

def condense_context(context: str, max_length: int = 8000) -> str:
    """
    Condense context to a maximum length by removing less important parts.
    
    This is a simple implementation that removes blank lines and trims
    the context to max_length.
    """
    if len(context) <= max_length:
        return context
    
    # Split into lines and remove blank lines
    lines = context.split('\n')
    non_blank_lines = [line for line in lines if line.strip()]
    
    # Rejoin with single blank lines between sections
    condensed = '\n'.join(non_blank_lines)
    
    # If still too long, truncate
    if len(condensed) > max_length:
        condensed = condensed[:max_length-3] + '...'
    
    return condensed

def get_context_summary(files: List[Tuple[str, str]]) -> str:
    """
    Generate a summary of the context files.
    """
    if not files:
        return "No files in context."
    
    summary = f"Context includes {len(files)} files:\n"
    for file_path, _ in files[:10]:  # Show first 10 files
        summary += f"  - {file_path}\n"
    
    if len(files) > 10:
        summary += f"  ... and {len(files) - 10} more files\n"
    
    return summary
