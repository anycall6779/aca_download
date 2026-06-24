"""
_install_edgedriver.py
Edge WebDriver 를 webdriver-manager 로 자동 설치 후
지정 디렉토리에 msedgedriver.exe 를 복사하는 헬퍼 스크립트.
Usage: python _install_edgedriver.py <target_dir>
"""
import sys
import shutil
import os

def main():
    target_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    dst = os.path.join(target_dir, "msedgedriver.exe")

    try:
        from webdriver_manager.microsoft import EdgeChromiumDriverManager
        path = EdgeChromiumDriverManager().install()
        shutil.copy2(path, dst)
        print(f" Edge WebDriver ready: {dst}")
    except Exception as e:
        print(f" [WARN] EdgeDriver install failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
