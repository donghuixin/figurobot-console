"""Figurobot 串口执行器（在设备上由 robot_bridge.py 调用）。

通过环境变量 SERIAL_HEX_FRAMES 传入帧列表（JSON 数组），执行后输出：
  PORT:<path>     找到的目标串口
  HEX:<hex>       读到的串口原始字节
  SERIAL_ERROR:<msg>  串口错误
"""
import os, sys, time, termios, fcntl, glob, json


def crc16(d):
    """CRC-16 IBM/ANSI (poly 0x8005, 初始值 0，官方 demo 标准)。"""
    c = 0
    for b in d:
        c ^= (b << 8) & 0xFFFF
        for _ in range(8):
            c = ((c << 1) ^ 0x8005) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return [c & 0xFF, (c >> 8) & 0xFF]


def ping(did):
    body = [0xFF, 0x00, 0xFD, 0x00, did, 3, 0, 1]
    return bytes(body + crc16(body))


def openp(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[2] = (a[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.PARODD)) \
           | termios.CS8 | termios.CREAD | termios.CLOCAL
    a[0] = 0
    a[1] = 0
    a[3] = 0
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 1
    a[4] = termios.B1000000
    a[5] = termios.B1000000
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def drain(fd, max_iters=20):
    out = b''
    for _ in range(max_iters):
        try:
            c = os.read(fd, 512)
            if not c:
                break
            out += c
        except Exception:
            break
    return out


def find_port():
    cands = glob.glob('/dev/serial/by-id/usb-1a86*') + glob.glob('/dev/ttyACM*')
    seen = set()
    ports = []
    for p in cands:
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            ports.append(real)
    for p in ports:
        try:
            fd = openp(p)
            os.write(fd, ping(1))
            time.sleep(0.15)
            out = drain(fd)
            os.close(fd)
            if b'\xfd\x00' in out:
                return p
        except Exception:
            pass
    return None


def main():
    read_timeout = float(os.environ.get('SERIAL_READ_TIMEOUT', '0.4'))
    frames_hex = os.environ.get('SERIAL_HEX_FRAMES', '[]')
    one_by_one = os.environ.get('SERIAL_ONE_BY_ONE', '0') == '1'
    try:
        frames = json.loads(frames_hex)
    except Exception as e:
        print('SERIAL_ERROR:bad_frames_json:{}'.format(e))
        return
    target = os.environ.get('SERIAL_PORT') or find_port()
    if target is None:
        print('SERIAL_ERROR:no_motor_serial')
        return
    try:
        fd = openp(target)
        if one_by_one:
            # 逐个模式：发一帧、等一帧、读一帧（半双工标准）。
            # 批量发送时 motion_main 独占串口会抢走响应，逐个模式窗口短、更可靠。
            collected = b''
            for f in frames:
                os.write(fd, bytes.fromhex(f))
                time.sleep(0.06)
                out = drain(fd, max_iters=10)
                collected += out
            print('PORT:' + target)
            print('HEX:' + collected.hex())
        else:
            for f in frames:
                os.write(fd, bytes.fromhex(f))
                time.sleep(0.04)
            time.sleep(read_timeout)
            out = drain(fd, max_iters=80)
            print('PORT:' + target)
            print('HEX:' + out.hex())
        os.close(fd)
    except Exception as e:
        print('SERIAL_ERROR:' + str(e))


if __name__ == '__main__':
    main()
