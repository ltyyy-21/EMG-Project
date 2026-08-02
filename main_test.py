import sys
import asyncio
import struct
import csv
import time
import os
from collections import deque

import winsound
import pyttsx3
import numpy as np
import matplotlib.pyplot as plt

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QWidget,
    QLabel,
    QComboBox,
    QLineEdit,
    QGroupBox,
    QInputDialog,
)
from PyQt6.QtCore import Qt

from bleak import BleakScanner, BleakClient
from qasync import QEventLoop
import pyqtgraph as pg


# =========================================================
# 基本設定：要和 Arduino BLE 程式一致
# =========================================================
DEVICE_NAME = "EMG-Logger"
CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"

SAMPLES_PER_PACKET = 64

# Arduino 新版封包格式：
# 4 bytes firstIndex
# 2 bytes battery_mV
# 2 bytes battery_percent
# 64 * 2 bytes EMG
PACKET_SIZE = 4 + 2 + 2 + SAMPLES_PER_PACKET * 2  # 136 bytes

SAMPLE_RATE = 1000  # Hz

# 根目錄統一為桌面上的 EMG_Data 檔案夾
BASE_FOLDER = os.path.join(
    os.path.expanduser("~"),
    "Desktop",
    "EMG_Data"
)

# =========================================================
# 自訂異常：用於中斷當前 Trial 並安全倒帶重測
# =========================================================
class TrialResetException(Exception):
    pass


class EMGSystem(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("自製 EMG + 握力器 MVC 同步採集系統 v8.0 (自動分類與命名升級版)")
        self.resize(1350, 880)

        os.makedirs(BASE_FOLDER, exist_ok=True)

        self.client = None
        self.is_previewing = False
        self.should_log_to_raw = False

        # 控制旗標
        self.is_paused = False          # 是否處於暫停狀態
        self.reset_requested = False     # 是否請求放棄當次、返回上一步

        self.data_buffer = deque(maxlen=SAMPLE_RATE * 5)
        self.trial_data = []
        self.preview_count = 0

        self.mvc_values = [0.0, 0.0, 0.0]

        self.latest_battery_mv = 0
        self.latest_battery_percent = 0
        self.latest_emg = 0
        self.latest_first_index = 0

        self._build_ui()
        self.update_target_force()

    # =====================================================
    # UI
    # =====================================================
    def _build_ui(self):
        main_layout = QVBoxLayout()

        self.status_label = QLabel("狀態：等待連線")
        self.status_label.setStyleSheet(
            "font-size: 16pt; color: #2c3e50; font-weight: bold;"
        )
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        main_layout.addWidget(self.status_label)

        info_group = QGroupBox("實驗基本資訊")
        info_layout = QHBoxLayout()

        info_layout.addWidget(QLabel("受試者 ID:"))
        self.subject_id = QLineEdit("S01")
        info_layout.addWidget(self.subject_id)

        info_layout.addWidget(QLabel("肌肉部位 (簡寫):"))
        self.muscle_input = QLineEdit("FF")  # 預設為簡寫 FF (Forearm Flexor)
        self.muscle_input.setPlaceholderText("例如: FF")
        info_layout.addWidget(self.muscle_input)

        # 新增：手動切換內外側 (I/O) 的下拉選單
        info_layout.addWidget(QLabel("測試位置 (I/O):"))
        self.position_combo = QComboBox()
        self.position_combo.addItems(["O (外側 / Outer)", "I (內側 / Inner)"])
        info_layout.addWidget(self.position_combo)

        info_layout.addWidget(QLabel("電池:"))
        self.battery_label = QLabel("-- mV / -- %")
        self.battery_label.setStyleSheet(
            "font-size: 13pt; color: #16a085; font-weight: bold;"
        )
        info_layout.addWidget(self.battery_label)

        info_layout.addWidget(QLabel("即時 EMG:"))
        self.emg_label = QLabel("--")
        self.emg_label.setStyleSheet(
            "font-size: 13pt; color: #8e44ad; font-weight: bold;"
        )
        info_layout.addWidget(self.emg_label)

        info_group.setLayout(info_layout)
        main_layout.addWidget(info_group)

        mvc_group = QGroupBox("MVC 三次測試結果")
        mvc_layout = QHBoxLayout()

        mvc_layout.addWidget(QLabel("MVC 第 1 次 kg:"))
        self.mvc1_input = QLineEdit("0.00")
        self.mvc1_input.setReadOnly(True)
        mvc_layout.addWidget(self.mvc1_input)

        mvc_layout.addWidget(QLabel("MVC 第 2 次 kg:"))
        self.mvc2_input = QLineEdit("0.00")
        self.mvc2_input.setReadOnly(True)
        mvc_layout.addWidget(self.mvc2_input)

        mvc_layout.addWidget(QLabel("MVC 第 3 次 kg:"))
        self.mvc3_input = QLineEdit("0.00")
        self.mvc3_input.setReadOnly(True)
        mvc_layout.addWidget(self.mvc3_input)

        mvc_layout.addWidget(QLabel("100% MVC 最大值 kg:"))
        self.ref_input = QLineEdit("0.00")
        self.ref_input.setReadOnly(True)
        self.ref_input.setStyleSheet(
            "font-size: 14pt; color: #e67e22; font-weight: bold;"
        )
        mvc_layout.addWidget(self.ref_input)

        mvc_layout.addWidget(QLabel("目前目標握力 kg:"))
        self.target_force_input = QLineEdit("0.00")
        self.target_force_input.setReadOnly(True)
        self.target_force_input.setStyleSheet(
            "font-size: 14pt; color: #2980b9; font-weight: bold;"
        )
        mvc_layout.addWidget(self.target_force_input)

        mvc_group.setLayout(mvc_layout)
        main_layout.addWidget(mvc_group)

        ctrl_group = QGroupBox("測試控制")
        ctrl_layout = QVBoxLayout()
        
        btn_layout1 = QHBoxLayout()
        self.connect_btn = QPushButton("1. 連接藍牙")
        self.connect_btn.clicked.connect(self.start_connect_task)
        btn_layout1.addWidget(self.connect_btn)

        self.preview_btn = QPushButton("即時預覽波形")
        self.preview_btn.clicked.connect(self.toggle_preview_task)
        self.preview_btn.setEnabled(False)
        btn_layout1.addWidget(self.preview_btn)

        self.mvc_test_btn = QPushButton("2. 開始 MVC 三次測試")
        self.mvc_test_btn.clicked.connect(self.start_mvc_test_task)
        self.mvc_test_btn.setEnabled(False)
        btn_layout1.addWidget(self.mvc_test_btn)

        btn_layout1.addWidget(QLabel("階梯測試強度:"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(["30% MVC", "60% MVC", "100% MVC"])
        self.level_combo.currentIndexChanged.connect(self.update_target_force)
        btn_layout1.addWidget(self.level_combo)

        self.start_task_btn = QPushButton("3. 開始握力器階梯測試")
        self.start_task_btn.clicked.connect(self.start_controlled_force_task)
        self.start_task_btn.setEnabled(False)
        btn_layout1.addWidget(self.start_task_btn)
        
        btn_layout2 = QHBoxLayout()
        self.pause_btn = QPushButton("⏸ 暫停 / ▶ 恢復")
        self.pause_btn.setStyleSheet("background-color: #f1c40f; font-weight: bold;")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.pause_btn.setEnabled(False)
        btn_layout2.addWidget(self.pause_btn)

        self.reset_step_btn = QPushButton("⏮ 放棄當次，返回重測")
        self.reset_step_btn.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold;")
        self.reset_step_btn.clicked.connect(self.request_reset_step)
        self.reset_step_btn.setEnabled(False)
        btn_layout2.addWidget(self.reset_step_btn)

        ctrl_layout.addLayout(btn_layout1)
        ctrl_layout.addLayout(btn_layout2)
        ctrl_group.setLayout(ctrl_layout)
        main_layout.addWidget(ctrl_group)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_widget.setLabel("left", "ADC Raw 0~1023")
        self.plot_widget.setLabel("bottom", "Sample")
        self.plot_widget.setYRange(0, 1023)
        self.curve = self.plot_widget.plot(pen=pg.mkPen("b", width=1.5))
        main_layout.addWidget(self.plot_widget)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    # =====================================================
    # 暫停與重測按鍵事件處理
    # =====================================================
    def toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.status_label.setText("【已暫停】系統已凍結，請調整完畢後再次點擊恢復。")
        else:
            self.status_label.setText("【已恢復】繼續進行實驗。")

    def request_reset_step(self):
        self.reset_requested = True
        self.is_paused = False 
        print("使用者請求：放棄當次數據並重測上一步")

    # =====================================================
    # 語音系統
    # =====================================================
    def speak_direct(self, text):
        try:
            print("語音播放：", text)
            engine = pyttsx3.init()
            engine.setProperty("rate", 145)
            engine.setProperty("volume", 1.0)
            engine.say(str(text))
            engine.runAndWait()
            engine.stop()
            time.sleep(0.25)
        except Exception as e:
            print("語音播放錯誤：", e)

    def speak(self, text):
        self.speak_direct(text)

    async def speak_and_wait(self, text):
        if self.reset_requested:
            raise TrialResetException()
        self.speak_direct(text)
        await asyncio.sleep(0.1)

    async def voice_countdown(self, seconds, prefix_text=""):
        seconds = int(seconds)
        number_words = {
            10: "Ten", 9: "Nine", 8: "Eight", 7: "Seven", 6: "Six",
            5: "Five", 4: "Four", 3: "Three", 2: "Two", 1: "One",
        }

        for sec in range(seconds, 0, -1):
            start_time = time.perf_counter()

            while self.is_paused:
                self.should_log_to_raw = False  
                await asyncio.sleep(0.1)
                if self.reset_requested:
                    raise TrialResetException()

            if self.reset_requested:
                raise TrialResetException()

            if prefix_text and ("放鬆" in prefix_text or "最大" in prefix_text or "發力" in prefix_text):
                if "休息" not in prefix_text:
                    self.should_log_to_raw = True

            if prefix_text:
                self.status_label.setText(f"{prefix_text}：剩 {sec} 秒")
                QApplication.processEvents()

            await self.speak_and_wait(number_words.get(sec, str(sec)))

            elapsed = time.perf_counter() - start_time
            sleep_time = max(0, 1.0 - elapsed)
            
            steps = int(sleep_time / 0.1)
            for _ in range(steps):
                if self.reset_requested:
                    raise TrialResetException()
                while self.is_paused:
                    self.should_log_to_raw = False
                    await asyncio.sleep(0.1)
                    if self.reset_requested:
                        raise TrialResetException()
                await asyncio.sleep(0.1)

    def clear_speech_queue(self):
        pass

    # =====================================================
    # BLE 連線
    # =====================================================
    def start_connect_task(self):
        asyncio.create_task(self.connect_ble())

    async def connect_ble(self):
        try:
            self.status_label.setText(f"正在搜尋 {DEVICE_NAME}...")
            QApplication.processEvents()

            device = await BleakScanner.find_device_by_name(
                DEVICE_NAME,
                timeout=10.0
            )

            if not device:
                self.status_label.setText(
                    f"找不到 {DEVICE_NAME}，請確認 XIAO 已開機並已燒錄新版 BLE 程式"
                )
                return

            self.client = BleakClient(device)
            await self.client.connect()

            self.status_label.setText("已連線，自製 EMG 準備完成")
            self.speak("EMG system ready")

            self.preview_btn.setEnabled(True)
            self.mvc_test_btn.setEnabled(True)
            self.connect_btn.setEnabled(False)

        except Exception as e:
            self.status_label.setText(f"連線失敗：{e}")

    async def _ensure_notify_started(self):
        if self.client is None or not self.client.is_connected:
            raise RuntimeError("尚未連接藍牙")

        try:
            await self.client.start_notify(CHAR_UUID, self.ble_data_handler)
        except Exception as e:
            print("start_notify 狀態：", e)

    async def _stop_notify_safe(self):
        if self.client is not None and self.client.is_connected:
            try:
                await self.client.stop_notify(CHAR_UUID)
            except Exception as e:
                print("stop_notify 狀態：", e)

    # =====================================================
    # 即時預覽波形
    # =====================================================
    def toggle_preview_task(self):
        asyncio.create_task(self.toggle_preview())

    async def toggle_preview(self):
        if self.client is None or not self.client.is_connected:
            self.status_label.setText("尚未連接藍牙")
            return

        if not self.is_previewing:
            self.is_previewing = True
            self.should_log_to_raw = False
            self.data_buffer.clear()
            self.preview_count = 0

            self.preview_btn.setText("停止預覽")
            self.mvc_test_btn.setEnabled(False)
            self.start_task_btn.setEnabled(False)

            await self._ensure_notify_started()
            self.status_label.setText("即時預覽中：放鬆與出力時，波形應該會明顯改變")
        else:
            self.is_previewing = False
            self.preview_btn.setText("即時預覽波形")

            await self._stop_notify_safe()

            self.mvc_test_btn.setEnabled(True)
            self.start_task_btn.setEnabled(self.get_mvc_kg() > 0)
            self.status_label.setText("已停止預覽")

    # =====================================================
    # MVC 三次測試
    # =====================================================
    def start_mvc_test_task(self):
        asyncio.create_task(self.run_mvc_three_trials())

    async def run_mvc_three_trials(self):
        if self.client is None or not self.client.is_connected:
            self.status_label.setText("尚未連接藍牙")
            return

        self.clear_speech_queue()

        self.mvc_test_btn.setEnabled(False)
        self.start_task_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.reset_step_btn.setEnabled(True)

        self.mvc_values = [0.0, 0.0, 0.0]
        self.mvc1_input.setText("0.00")
        self.mvc2_input.setText("0.00")
        self.mvc3_input.setText("0.00")
        self.ref_input.setText("0.00")
        self.target_force_input.setText("0.00")

        try:
            await self._ensure_notify_started()

            trial_index = 1
            while trial_index <= 3:
                self.is_paused = False
                self.reset_requested = False
                
                try:
                    self.trial_data = []
                    self.data_buffer.clear()

                    self.status_label.setText(f"MVC Trial {trial_index}/3：準備開始")
                    trial_words = {1: "one", 2: "two", 3: "three"}

                    await self.speak_and_wait(f"Trial {trial_words[trial_index]} Ready")

                    self.should_log_to_raw = True
                    winsound.Beep(1200, 150)

                    await self.voice_countdown(5, f"MVC Trial {trial_index}/3 準備放鬆")

                    await self.speak_and_wait("Start")
                    winsound.Beep(1200, 150)

                    await self.voice_countdown(5, f"MVC Trial {trial_index}/3 最大握力")

                    await self.speak_and_wait("Relax")
                    winsound.Beep(700, 150)

                    await self.voice_countdown(10, f"MVC Trial {trial_index}/3 休息放鬆")

                    self.should_log_to_raw = False

                    kg_value = self.ask_mvc_value(trial_index)
                    self.mvc_values[trial_index - 1] = kg_value

                    if trial_index == 1:
                        self.mvc1_input.setText(f"{kg_value:.2f}")
                    elif trial_index == 2:
                        self.mvc2_input.setText(f"{kg_value:.2f}")
                    else:
                        self.mvc3_input.setText(f"{kg_value:.2f}")

                    if self.trial_data:
                        self.save_scientific_data(
                            condition_str="MVC",
                            trial_num=trial_index,
                            data_list=self.trial_data,
                            mvc_level="100% MVC",
                            target_force_kg=kg_value,
                            test_type="MVC",
                            manual_mvc_trial_kg=kg_value,
                        )

                    if trial_index < 3:
                        await self.speak_and_wait("Relax")
                        winsound.Beep(700, 150)
                        await self.voice_countdown(10, "MVC Trial 間休息")
                    
                    trial_index += 1

                except TrialResetException:
                    self.should_log_to_raw = False
                    self.trial_data = []
                    self.data_buffer.clear()
                    winsound.Beep(400, 500)  
                    self.status_label.setText(f"⚠️ 已取消並擦除 MVC Trial {trial_index} 的錯誤數據，準備重新測試。")
                    await asyncio.sleep(2.0)
                    continue

            await self._stop_notify_safe()

            max_mvc = max(self.mvc_values)
            self.ref_input.setText(f"{max_mvc:.2f}")
            self.update_target_force()

            self.status_label.setText(f"MVC 三次測試完成：100% MVC = {max_mvc:.2f} kg")
            self.speak("Test finished")
            self.start_task_btn.setEnabled(True)

        except Exception as e:
            self.status_label.setText(f"MVC 測試發生錯誤：{e}")

        finally:
            self.should_log_to_raw = False
            self.mvc_test_btn.setEnabled(True)
            self.preview_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.reset_step_btn.setEnabled(False)

    def ask_mvc_value(self, trial_index):
        while True:
            value, ok = QInputDialog.getDouble(
                self,
                f"輸入 MVC 第 {trial_index} 次最大握力",
                f"請輸入 MVC 第 {trial_index} 次握力器顯示的最大值 kg：",
                0.0, 0.0, 300.0, 2,
            )
            if ok and value > 0:
                return value
            self.status_label.setText(f"MVC 第 {trial_index} 次握力輸入無效，請重新輸入大於 0 的數字")

    # =====================================================
    # MVC 與目標握力計算
    # =====================================================
    def get_mvc_trials(self):
        return self.mvc_values[0], self.mvc_values[1], self.mvc_values[2]

    def get_mvc_kg(self):
        try:
            return float(self.ref_input.text())
        except ValueError:
            return 0.0

    def get_level_percent(self):
        level_text = self.level_combo.currentText()
        if "30" in level_text: return 0.30
        if "60" in level_text: return 0.60
        if "100" in level_text: return 1.00
        return 0.0

    def update_target_force(self):
        mvc_kg = self.get_mvc_kg()
        percent = self.get_level_percent()
        target_kg = mvc_kg * percent

        self.target_force_input.setText(f"{target_kg:.2f}")

        if hasattr(self, "status_label"):
            self.status_label.setText(
                f"目前設定：{self.level_combo.currentText()}，目標握力 {target_kg:.2f} kg"
            )

    # =====================================================
    # 階梯測試
    # =====================================================
    def start_controlled_force_task(self):
        asyncio.create_task(self.run_controlled_force())

    async def run_controlled_force(self):
        if self.client is None or not self.client.is_connected:
            self.status_label.setText("尚未連接藍牙")
            return

        self.clear_speech_queue()
        mvc_kg = self.get_mvc_kg()

        if mvc_kg <= 0:
            self.status_label.setText("請先完成 MVC 三次測試")
            return

        self.update_target_force()

        level_text = self.level_combo.currentText()
        # 根據選單項目自動提取 30, 60, 100 字串作為檔名條件
        condition_str = "30" if "30" in level_text else ("60" if "60" in level_text else "100")
        target_force_kg = float(self.target_force_input.text())

        self.mvc_test_btn.setEnabled(False)
        self.start_task_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.reset_step_btn.setEnabled(True)

        try:
            await self._ensure_notify_started()

            trial_index = 1
            while trial_index <= 3:
                self.is_paused = False
                self.reset_requested = False
                
                try:
                    self.trial_data = []
                    self.data_buffer.clear()

                    self.status_label.setText(
                        f"{level_text} Trial {trial_index}/3，目標握力 {target_force_kg:.2f} kg"
                    )

                    trial_words = {1: "one", 2: "two", 3: "three"}
                    await self.speak_and_wait(f"Trial {trial_words[trial_index]} Ready")

                    self.should_log_to_raw = True
                    winsound.Beep(1200, 150)

                    await self.voice_countdown(5, f"{level_text} Trial {trial_index}/3 準備放鬆")

                    await self.speak_and_wait("Start")
                    winsound.Beep(1000, 150)

                    await self.voice_countdown(5, f"{level_text} Trial {trial_index}/3 發力中")

                    await self.speak_and_wait("Relax")
                    winsound.Beep(700, 150)

                    await self.voice_countdown(10, f"{level_text} Trial {trial_index}/3 休息放鬆")

                    self.should_log_to_raw = False

                    if self.trial_data:
                        self.save_scientific_data(
                            condition_str=condition_str,
                            trial_num=trial_index,
                            data_list=self.trial_data,
                            mvc_level=level_text,
                            target_force_kg=target_force_kg,
                            test_type="Controlled_Force",
                            manual_mvc_trial_kg="",
                        )

                    if trial_index < 3:
                        await self.speak_and_wait("Relax")
                        winsound.Beep(700, 150)
                        await self.voice_countdown(10, "Trial 間休息")
                    
                    trial_index += 1

                except TrialResetException:
                    self.should_log_to_raw = False
                    self.trial_data = []
                    self.data_buffer.clear()
                    winsound.Beep(400, 500)
                    self.status_label.setText(f"⚠️ 已取消並擦除 {level_text} Trial {trial_index} 的錯誤數據，準備重新測試。")
                    await asyncio.sleep(2.0)
                    continue

            await self._stop_notify_safe()
            self.status_label.setText(f"{level_text} 階梯測試完成")
            self.speak("Test finished")

        except Exception as e:
            self.status_label.setText(f"階梯測試發生錯誤：{e}")

        finally:
            self.should_log_to_raw = False
            self.mvc_test_btn.setEnabled(True)
            self.start_task_btn.setEnabled(True)
            self.preview_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.reset_step_btn.setEnabled(False)

    # =====================================================
    # BLE 資料接收與即時圖
    # =====================================================
    def ble_data_handler(self, sender, data):
        if len(data) < PACKET_SIZE:
            return

        packet = data[:PACKET_SIZE]

        try:
            unpacked = struct.unpack("<IHH64H", packet)
        except struct.error as e:
            print("封包解析錯誤：", e, "len=", len(data))
            return

        first_index = unpacked[0]
        battery_mv = unpacked[1]
        battery_percent = unpacked[2]
        values = unpacked[3:]

        self.latest_first_index = first_index
        self.latest_battery_mv = battery_mv
        self.latest_battery_percent = battery_percent
        self.latest_emg = values[0] if values else 0

        self.battery_label.setText(f"{battery_mv} mV / {battery_percent}%")
        self.emg_label.setText(str(self.latest_emg))

        if not self.is_paused:
            if self.is_previewing or self.should_log_to_raw:
                for i, val in enumerate(values):
                    self.data_buffer.append(val)

                    if self.should_log_to_raw:
                        self.trial_data.append(
                            {
                                "index": int(first_index + i),
                                "adc_raw": int(val),
                                "battery_mv": int(battery_mv),
                                "battery_percent": int(battery_percent),
                            }
                        )

                self.curve.setData(list(self.data_buffer))

                if self.is_previewing:
                    self.preview_count += len(values)
                    self.status_label.setText(
                        f"即時預覽中 | Battery: {battery_mv} mV, {battery_percent}% | "
                        f"EMG: {self.latest_emg} | 已接收 {self.preview_count} samples"
                    )

    # =====================================================
    # 儲存 CSV 和 PNG (按新階層目錄與命名格式)
    # =====================================================
    def save_scientific_data(
        self,
        condition_str,   # "MVC"、"30"、"60"、"100"
        trial_num,       # 1, 2, 3
        data_list,
        mvc_level,
        target_force_kg,
        test_type,
        manual_mvc_trial_kg="",
    ):
        if not data_list:
            self.status_label.setText("沒有資料可儲存")
            return

        # 1. 讀取 UI 欄位元數據
        sid = self.subject_id.text().strip() or "UnknownSubject"
        muscle_short = self.muscle_input.text().strip() or "UnknownMuscle"
        
        # 提取內外側標籤 (I 或 O)
        pos_text = self.position_combo.currentText()
        loc_label = "O" if pos_text.startswith("O") else "I"
        
        # 獲取當前日期字串格式 (MMDD，如 0608)
        current_date = time.strftime("%m%d")

        # 2. 建構新四層階層式路徑
        # 格式：EMG_Data / 受試者編號 / 日期 / 測試部位 / 系統
        diy_dir = os.path.join(BASE_FOLDER, sid, current_date, muscle_short, "DIY")
        bts_dir = os.path.join(BASE_FOLDER, sid, current_date, muscle_short, "BTS")
        
        os.makedirs(diy_dir, exist_ok=True)
        os.makedirs(bts_dir, exist_ok=True)  # 自動同步生成空 BTS 資料夾

        # 3. 依照「位置_條件_測次」生成標準檔名
        # 範例：O_MVC_T1.csv
        base_filename = f"{loc_label}_{condition_str}_T{trial_num}"
        csv_path = os.path.join(diy_dir, f"{base_filename}.csv")
        png_path = os.path.join(diy_dir, f"{base_filename}.png")

        # 4. 資料科學運算加工 (以下維持原先成熟的預處理算法)
        indices = np.array([row["index"] for row in data_list], dtype=np.int64)
        data = np.array([row["adc_raw"] for row in data_list], dtype=np.float64)
        battery_mv_arr = np.array([row["battery_mv"] for row in data_list], dtype=np.int32)
        battery_percent_arr = np.array([row["battery_percent"] for row in data_list], dtype=np.int32)

        bs_count = min(len(data), SAMPLE_RATE)
        if bs_count > 0:
            baseline = float(np.mean(data[:bs_count]))
        else:
            baseline = 0.0

        centered = data - baseline

        if len(centered) > 0:
            rms = float(np.sqrt(np.mean(centered ** 2)))
        else:
            rms = 0.0

        mvc1, mvc2, mvc3 = self.get_mvc_trials()
        mvc_kg = self.get_mvc_kg()

        # 寫入科學 CSV 報表
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["subject_id", sid])
            writer.writerow(["muscle", muscle_short])
            writer.writerow(["test_type", test_type])
            writer.writerow(["mvc_trial1_kg", f"{mvc1:.2f}"])
            writer.writerow(["mvc_trial2_kg", f"{mvc2:.2f}"])
            writer.writerow(["mvc_trial3_kg", f"{mvc3:.2f}"])
            writer.writerow(["mvc_kg_100_percent", f"{mvc_kg:.2f}"])
            writer.writerow(["manual_mvc_trial_kg", manual_mvc_trial_kg])
            writer.writerow(["mvc_level", mvc_level])
            writer.writerow(["target_force_kg", f"{float(target_force_kg):.2f}"])
            writer.writerow(["sample_rate", SAMPLE_RATE])
            writer.writerow(["baseline", f"{baseline:.4f}"])
            writer.writerow(["rms", f"{rms:.4f}"])
            writer.writerow(["battery_mv_last", int(battery_mv_arr[-1])])
            writer.writerow(["battery_percent_last", int(battery_percent_arr[-1])])
            writer.writerow(["record_time", time.strftime("%Y-%m-%d %H:%M:%S")])
            writer.writerow([])
            writer.writerow(
                [
                    "sample_index", "time_sec", "adc_raw", "emg_centered",
                    "battery_mv", "battery_percent", "mvc_kg_100_percent",
                    "mvc_level", "target_force_kg", "test_type",
                ]
            )

            for i, val in enumerate(data):
                writer.writerow(
                    [
                        int(indices[i]),
                        f"{i / SAMPLE_RATE:.4f}",
                        int(val),
                        f"{centered[i]:.4f}",
                        int(battery_mv_arr[i]),
                        int(battery_percent_arr[i]),
                        f"{mvc_kg:.2f}",
                        mvc_level,
                        f"{float(target_force_kg):.2f}",
                        test_type,
                    ]
                )

        # 畫圖並導出高解析度波形對比圖
        time_axis = np.linspace(0, len(data) / SAMPLE_RATE, len(data))
        plt.figure(figsize=(12, 5), dpi=100)
        plt.plot(time_axis, centered, lw=0.7)
        plt.axhline(0, color="gray", linestyle="--", linewidth=0.8)
        plt.grid(True, alpha=0.3)
        plt.title(
            f"DIY EMG | {sid} | {muscle_short} ({loc_label}) | {base_filename} | RMS {rms:.2f}"
        )
        plt.xlabel("Time (s)")
        plt.ylabel("Centered EMG ADC")
        plt.savefig(png_path)
        plt.close()

        self.status_label.setText(f"數據保存成功！已歸類至：{diy_dir}")
        print("CSV 已存檔至:", csv_path)
        print("PNG 已存檔至:", png_path)


# =====================================================
# 主程式
# =====================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = EMGSystem()
    window.show()

    with loop:
        loop.run_forever()

