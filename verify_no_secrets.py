#!/usr/bin/env python3
import os
import sys
import re

def check_gitignore():
    print("Checking .gitignore...")
    if not os.path.exists(".gitignore"):
        print("❌ .gitignore file not found!")
        return False
    
    with open(".gitignore", "r", encoding="utf-8") as f:
        content = f.read()
        
    required = [".env", "token.json"]
    missing = []
    for req in required:
        # Match lines that ignore the file (uncommented)
        if not re.search(r"^\s*" + re.escape(req) + r"\b", content, re.MULTILINE):
            missing.append(req)
            
    if missing:
        print(f"❌ .gitignore is missing rules for: {', '.join(missing)}")
        return False
        
    print("✅ .gitignore has correct ignore rules.")
    return True

def scan_files():
    print("Scanning code files for hardcoded secrets...")
    # Regex patterns for matching common hardcoded credentials in python scripts
    # Skip matching env variable default fallbacks or examples
    patterns = {
        "email/account": re.compile(r"account\s*=\s*['\"]([^'\"]+@[^'\"]+\.[^'\"]+)['\"]"),
        "password": re.compile(r"password\s*=\s*['\"]([^'\"]{4,})['\"]"),
        "token/key": re.compile(r"(telegram_token|bot_token|mqtt_password)\s*=\s*['\"]([^'\"]{6,})['\"]"),
    }
    
    clean = True
    for root, dirs, files in os.walk("."):
        # Exclude common directories
        dirs[:] = [d for d in dirs if d not in [".git", "__pycache__", "venv", ".venv"]]
        
        for file in files:
            if not file.endswith(".py") or file == "verify_no_secrets.py":
                continue
                
            filepath = os.path.join(root, file)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception as e:
                print(f"⚠️ Could not read {filepath}: {e}")
                continue
                
            for idx, line in enumerate(lines, 1):
                # Skip comments
                if line.strip().startswith("#"):
                    continue
                    
                for label, pattern in patterns.items():
                    match = pattern.search(line)
                    if match:
                        val = match.group(1)
                        # Exclude placeholders
                        if val in ["your_username@email.com", "your_password", "your_mqtt_password", "your_bot_token", "abcd1234567890ef"]:
                            continue
                        print(f"❌ Possible hardcoded {label} found in {filepath}:{idx} -> {line.strip()}")
                        clean = False
                        
    if clean:
        print("✅ No hardcoded secrets found in code files.")
    return clean

def main():
    success = True
    if not check_gitignore():
        success = False
    if not scan_files():
        success = False
        
    if not success:
        print("\n❌ Security check FAILED! Please fix the errors above before continuing.")
        sys.exit(1)
    else:
        print("\n✅ Security check PASSED! Ready for commit/build.")
        sys.exit(0)

if __name__ == "__main__":
    main()
