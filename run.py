import os
import sys
import subprocess

def main():
    print("正在启动英语学习助手...")
    
    # 获取 Python 解释器路径
    python_path = sys.executable
    
    try:
        # 运行主程序
        process = subprocess.Popen(
            [python_path, "main.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # 实时输出日志
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                print(output.strip())
        
        # 检查是否有错误
        rc = process.poll()
        if rc != 0:
            print("\n程序异常退出，错误信息：")
            print(process.stderr.read())
    
    except Exception as e:
        print(f"启动失败: {str(e)}")
    
    input("\n按回车键退出...")

if __name__ == "__main__":
    main() 