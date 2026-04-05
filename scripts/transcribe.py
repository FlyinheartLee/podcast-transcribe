#!/usr/bin/env python3
"""
播客转逐字稿 - 完整工作流
支持：下载、切分、Whisper转写、断点续传、合并、清洗
"""
import os
import sys
import re
import glob
import subprocess
import json
import time
from datetime import datetime

# ============================================
# 配置
# ============================================
WORKSPACE = os.environ.get('WORKSPACE', os.path.expanduser('~/.openclaw/workspace-main'))
PODCASTS_DIR = os.path.join(WORKSPACE, "podcasts")
SPLIT_DIR = os.path.join(PODCASTS_DIR, "split")
MODEL_NAME = "large-v3"
CHUNK_DURATION = 2400  # 40分钟

# 确保目录存在
os.makedirs(PODCASTS_DIR, exist_ok=True)
os.makedirs(SPLIT_DIR, exist_ok=True)

# ============================================
# 工具函数
# ============================================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def run_cmd(cmd, timeout=300):
    """执行命令，返回 (success, output)"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "命令超时"
    except Exception as e:
        return False, str(e)

def get_audio_duration(audio_path):
    """获取音频时长（秒）"""
    cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
    success, output = run_cmd(cmd)
    if success:
        try:
            return float(output.strip())
        except:
            pass
    return 0

def extract_xiaoyuzhou_episode_id(url):
    """从小宇宙URL提取episode ID"""
    # 格式: https://www.xiaoyuzhoufm.com/episode/1234567890
    match = re.search(r'/episode/([a-zA-Z0-9]+)', url)
    if match:
        return match.group(1)
    # 格式: https://www.xiaoyuzhoufm.com/podcast/xxx/episode/1234567890
    match = re.search(r'/episode/([a-zA-Z0-9]+)', url)
    return match.group(1) if match else None

def download_xiaoyuzhou_audio(url, output_path):
    """下载小宇宙音频"""
    episode_id = extract_xiaoyuzhou_episode_id(url)
    if not episode_id:
        return False, "无法解析episode ID"
    
    # 小宇宙API
    api_url = f"https://www.xiaoyuzhoufm.com/api/v2/episode/{episode_id}"
    
    # 获取音频信息
    cmd = f'curl -s "{api_url}"'
    success, output = run_cmd(cmd)
    if not success:
        return False, f"API请求失败: {output}"
    
    try:
        data = json.loads(output)
        audio_url = data.get('audio_url', '')
        if not audio_url:
            return False, "未找到音频链接"
    except:
        return False, "解析JSON失败"
    
    # 下载音频
    log(f"开始下载音频: {audio_url[:50]}...")
    cmd = f'curl -L -o "{output_path}" "{audio_url}"'
    success, output = run_cmd(cmd, timeout=600)
    
    if success and os.path.getsize(output_path) > 10000:
        return True, "下载成功"
    else:
        return False, f"下载失败: {output[:200]}"

def generic_download(url, output_path):
    """通用下载（尝试获取页面中的音频链接）"""
    log(f"尝试下载: {url}")
    cmd = f'curl -L -o "{output_path}" "{url}" --max-time 300'
    success, output = run_cmd(cmd, timeout=320)
    
    if success and os.path.getsize(output_path) > 10000:
        return True, "下载成功"
    return False, "下载失败"

def split_audio(audio_path, output_dir):
    """切分音频"""
    duration = get_audio_duration(audio_path)
    if duration <= 0:
        return [], "无法获取音频时长"
    
    if duration <= 3600:  # ≤1小时不分段
        return [audio_path], "不分段"
    
    # 计算分段
    chunks = []
    start = 0
    part_num = 1
    
    while start < duration:
        end = min(start + CHUNK_DURATION, duration)
        output_file = os.path.join(output_dir, f"part_{part_num}.m4a")
        
        log(f"切分 part{part_num}: {start//60}-{end//60} 分钟")
        cmd = f'ffmpeg -y -i "{audio_path}" -ss {start} -to {end} -c copy "{output_file}" -nostats -loglevel error'
        success, _ = run_cmd(cmd)
        
        if success and os.path.exists(output_file):
            chunks.append(output_file)
        
        start = end
        part_num += 1
    
    return chunks, f"分成 {len(chunks)} 段"

# ============================================
# Whisper 转写（需要先安装whisper）
# ============================================
def transcribe_with_whisper(audio_path, output_path, language="zh"):
    """使用Whisper转写"""
    try:
        import whisper
    except ImportError:
        return False, "请先安装: pip install openai-whisper"
    
    log(f"加载模型 {MODEL_NAME}...")
    try:
        model = whisper.load_model(MODEL_NAME, device="cpu")
    except Exception as e:
        return False, f"模型加载失败: {e}"
    
    log(f"转写: {os.path.basename(audio_path)}")
    try:
        result = whisper.transcribe(model, audio_path, language=language, fp16=False)
        
        # 写入文本
        with open(output_path, "w", encoding="utf-8") as f:
            for seg in result["segments"]:
                text = seg["text"].strip()
                if text:
                    f.write(text + "\n")
        
        return True, f"转写完成，{len(result['segments'])} 个片段"
    except Exception as e:
        return False, f"转写失败: {e}"

def check_completed_parts(output_dir, prefix="part_"):
    """检查已完成的parts"""
    parts = sorted(glob.glob(os.path.join(output_dir, f"{prefix}*.txt")))
    completed = []
    for p in parts:
        if os.path.getsize(p) > 1024:  # >1KB
            completed.append(p)
    return completed

# ============================================
# LLM 清洗 - 由 Agent 在对话中调用自己的模型
# ============================================
CLEANUP_USER_PROMPT = """请整理以下逐字稿，遵循以下规则：

1. **删除语气助词**：嗯、啊、这个、那个、然后、其实、就是、基本上、大概、对吧、好吗、呢、嘛等无意义的填充词

2. **去除口水话**：重复的表述、口齿不清的碎片、重复确认的话、说了又纠正的话

3. **完善修正表达**：如果说话者说"不对/我重新说/刚才那个...不是...是..."，只保留最终正确的表达，去掉前面的错误部分

4. **整理逻辑顺序**：如果前后顺序明显错乱（话题跳跃或颠倒），重新排列使其符合逻辑

5. **补充完整**：如果一句话明显没说完但意思清楚，适当补充完整使其可读

6. **保留核心**：人名、专业术语、关键观点、独特见解、具体数据必须保留

7. **标注说话人**：如果有多人对话，用"【说话人A】"、"【说话人B】"标注

请直接输出整理后的内容，不要添加任何评论或解释。

---

逐字稿内容：
"""

def notify_cleanup_needed(merged_path):
    """提示 Agent 需要在对话中进行清洗"""
    with open(merged_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 输出清洗提示（让 Agent 知道需要在对话中完成清洗）
    log(f"\n✅ 转写完成！原始文件: {merged_path}")
    log("="*60)
    log("【请在对话中调用你的模型进行清洗】")
    log("="*60)
    log(f"\n将以下内容发送给模型:\n")
    log(f"{CLEANUP_USER_PROMPT}")
    log(f"<逐字稿内容，共 {len(content)} 字符>")
    log("\n模型输出后，保存为: {merged_path.replace('.txt', '_清洗.txt')}")
    log("="*60)

# ============================================
# 主流程
# ============================================
def main(url, title=None):
    """主流程"""
    log(f"开始处理: {url}")
    
    # 1. 确定文件名
    if not title:
        title = "podcast"
    safe_title = re.sub(r'[^\w\-_\u4e00-\u9fa5]', '_', title)[:50]
    base_name = safe_title
    
    audio_path = os.path.join(PODCASTS_DIR, f"{base_name}.m4a")
    
    # 2. 下载音频
    log("Step 1: 下载音频...")
    if "xiaoyuzhoufm" in url:
        success, msg = download_xiaoyuzhou_audio(url, audio_path)
    else:
        success, msg = generic_download(url, audio_path)
    
    if not success:
        log(f"下载失败: {msg}")
        return False, f"下载失败: {msg}"
    
    log(f"音频已保存: {audio_path}")
    
    # 3. 音频切分
    log("Step 2: 切分音频...")
    chunks, msg = split_audio(audio_path, SPLIT_DIR)
    log(f"切分结果: {msg}")
    
    if not chunks:
        return False, "音频切分失败"
    
    # 4. 转写（支持断点续传）
    log("Step 3: 转写音频...")
    output_files = []
    for i, chunk in enumerate(chunks, 1):
        part_name = f"part_{i}.txt"
        output_file = os.path.join(PODCASTS_DIR, part_name)
        
        # 检查是否已完成
        if os.path.exists(output_file) and os.path.getsize(output_file) > 1024:
            log(f"Part {i} 已完成，跳过")
            output_files.append(output_file)
            continue
        
        log(f"转写 part {i}/{len(chunks)}...")
        success, msg = transcribe_with_whisper(chunk, output_file)
        
        if success:
            output_files.append(output_file)
            log(f"Part {i} 完成")
        else:
            log(f"Part {i} 失败: {msg}")
            # 断点续传：继续下一个
            continue
    
    if not output_files:
        return False, "所有段落转写失败"
    
    # 5. 合并
    log("Step 4: 合并逐字稿...")
    merged_path = os.path.join(PODCASTS_DIR, f"{base_name}_逐字稿.txt")
    with open(merged_path, "w", encoding="utf-8") as out:
        for f in sorted(output_files):
            with open(f, "r", encoding="utf-8") as inp:
                out.write(inp.read() + "\n")
    
    log(f"合并完成: {merged_path}")
    
    # 6. LLM清洗（由 Agent 在对话中完成）
    log("Step 5: 提示在对话中进行 LLM 清洗...")
    notify_cleanup_needed(merged_path)
    
    log("✅ 全部完成!")
    return True, merged_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python transcribe.py <URL> [标题]")
        sys.exit(1)
    
    url = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else None
    
    success, msg = main(url, title)
    if success:
        print(f"\n完成: {msg}")
    else:
        print(f"\n失败: {msg}")
        sys.exit(1)