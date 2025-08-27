import sys
import socket
import threading
import json
import os
import subprocess
from PyQt6.QtCore import QThread, QObject, pyqtSignal, QDateTime, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QTextEdit, QLabel, QMessageBox, QFileDialog, QComboBox
)
from PyQt6.QtGui import QTextOption

# 定义文件接收状态
class ReceiverState:
    WAITING_FOR_HEADER = 0
    RECEIVING_FILE = 1

class ClientThread(QObject):
    update_log_signal = pyqtSignal(str)
    update_log_with_image_signal = pyqtSignal(str, str, bool)
    status_changed_signal = pyqtSignal(bool)
    connected_signal = pyqtSignal()
    disconnected_signal = pyqtSignal()
    online_users_updated = pyqtSignal(list)

    def __init__(self, ip, port, username, parent=None):
        super().__init__(parent)
        self.ip = ip
        self.port = port
        self.username = username
        self.is_connected = False
        self.client_socket = None
        self.receiver_state = ReceiverState.WAITING_FOR_HEADER
        self.file_info = None
        self.file_handle = None
        self.received_bytes = 0
        self.buffer = b""

    def connect_to_server(self):
        if self.is_connected:
            return

        self.update_log_signal.emit(f"正在以用戶 '{self.username}' 的身份連接伺服器...")
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.connect((self.ip, self.port))
            self.client_socket.settimeout(1)
            
            initial_message = {"type": "join", "username": self.username}
            self.client_socket.sendall(json.dumps(initial_message).encode("utf-8"))
            
            self.is_connected = True
            self.status_changed_signal.emit(True)
            self.connected_signal.emit()
            self.update_log_signal.emit(f"成功連接到 {self.ip}:{self.port}")
            self.receive_messages()

        except socket.error as e:
            self.update_log_signal.emit(f"連接伺服器失敗: {e}")
            self.stop_client()

    def receive_messages(self):
        while self.is_connected:
            try:
                data = self.client_socket.recv(4096)
                if not data:
                    break
                
                self.buffer += data

                if self.receiver_state == ReceiverState.WAITING_FOR_HEADER:
                    try:
                        message_str = self.buffer.decode("utf-8")
                        header_end_index = message_str.find('}')
                        
                        if header_end_index != -1:
                            header_json_str = message_str[:header_end_index + 1]
                            header = json.loads(header_json_str)

                            remaining_data = self.buffer[len(header_json_str.encode("utf-8")):]
                            self.buffer = remaining_data
                            
                            if header.get("type") == "message" and "sender" in header and "content" in header:
                                sender = header["sender"]
                                if sender == self.username and header.get("is_private") is not True:
                                    continue

                                content = header["content"]
                                is_private = header.get("is_private", False)
                                prefix = f"[私密消息] " if is_private else ""
                                self.update_log_signal.emit(f"{prefix}[{sender}] {content}")
                            
                            elif header.get("type") == "online_users" and "users" in header:
                                users = header["users"]
                                self.online_users_updated.emit(users)
                                self.update_log_signal.emit(f"在線用戶列表已更新: {', '.join(users)}")
                            
                            elif header.get("type") == "file_start" and "filename" in header and "size" in header:
                                filename = header["filename"]
                                size = header["size"]
                                filetype = header.get("filetype", "file")
                                sender = header.get("sender", "未知")
                                is_private = header.get("is_private", False)

                                if sender == self.username and is_private is not True:
                                    self.update_log_signal.emit(f"忽略來自自己的廣播文件: {filename}")
                                    self.file_info = header
                                    self.file_handle = open(os.devnull, "wb")
                                    self.received_bytes = 0
                                    self.receiver_state = ReceiverState.RECEIVING_FILE
                                    if self.buffer:
                                        self._handle_file_data(self.buffer)
                                        self.buffer = b""
                                    continue

                                self.update_log_signal.emit(f"準備接收來自 [{sender}] 的{'私密圖片' if is_private else '圖片' if filetype == 'image' else '文件'}: {filename} ({size} 字节)")

                                save_path = os.path.join(os.getcwd(), f"client_received_{os.path.basename(filename)}")
                                self.file_handle = open(save_path, "wb")
                                self.file_info = header
                                self.received_bytes = 0
                                self.receiver_state = ReceiverState.RECEIVING_FILE

                                if self.buffer:
                                    self._handle_file_data(self.buffer)
                                    self.buffer = b""
                            
                            else:
                                self.update_log_signal.emit(f"從伺服器收到: {header_json_str}")
                                self.buffer = b""
                        
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        # 這是最關鍵的修復點
                        # 如果解析失敗，說明緩衝區內的數據不完整或不純粹是JSON
                        # 什麼都不做，等待更多數據
                        self.update_log_signal.emit(f"嘗試解析JSON失敗，可能數據不完整，正在等待... Error: {e}")
                        pass
                
                if self.receiver_state == ReceiverState.RECEIVING_FILE:
                    self._handle_file_data(self.buffer)
                    self.buffer = b""
            
            except socket.timeout:
                continue
            except (socket.error, ConnectionResetError, BrokenPipeError):
                break
        
        self.update_log_signal.emit("與伺服器的連接已斷開。")
        self.stop_client()
    
    def _handle_file_data(self, data):
        if not self.file_handle:
            self.update_log_signal.emit("錯誤: 在接收文件數據時文件句柄為空。")
            self.receiver_state = ReceiverState.WAITING_FOR_HEADER
            self.buffer = b""
            return

        self.file_handle.write(data)
        self.received_bytes += len(data)

        if self.received_bytes >= self.file_info["size"]:
            self.file_handle.close()
            
            filetype = self.file_info.get("filetype", "file")
            filename = self.file_info["filename"]
            sender = self.file_info.get("sender", "未知")
            is_private = self.file_info.get("is_private", False)

            if self.file_handle.name != os.devnull:
                if filetype == "image":
                    save_path = os.path.join(os.getcwd(), f"client_received_{os.path.basename(filename)}")
                    self.update_log_with_image_signal.emit(save_path, sender, is_private)
                else:
                    self.update_log_signal.emit(f"文件接收完成: {self.file_info['filename']}")
            
            self.receiver_state = ReceiverState.WAITING_FOR_HEADER
            self.file_info = None
            self.file_handle = None
            self.received_bytes = 0

    def send_message(self, recipient, message):
        if not self.is_connected:
            self.update_log_signal.emit("未連接到伺服器，無法發送消息。")
            return
        
        try:
            msg_obj = {"type": "message", "sender": self.username, "content": message, "recipient": recipient}
            self.client_socket.sendall(json.dumps(msg_obj).encode("utf-8"))
        except (socket.error, BrokenPipeError):
            self.update_log_signal.emit("發送消息失敗，伺服器可能已斷開。")
            self.stop_client()

    def send_file(self, recipient, file_path, file_type="file"):
        if not self.is_connected:
            self.update_log_signal.emit("未連接到伺服器，無法發送文件。")
            return
        
        try:
            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)

            header = {"type": "file_start", "filename": filename, "size": file_size, "filetype": file_type, "sender": self.username, "recipient": recipient}
            self.client_socket.sendall(json.dumps(header).encode("utf-8"))

            with open(file_path, 'rb') as f:
                while True:
                    bytes_read = f.read(4096)
                    if not bytes_read:
                        break
                    self.client_socket.sendall(bytes_read)
            
            self.update_log_signal.emit(f"{'圖片' if file_type == 'image' else '文件'} {filename} 發送完畢。")
        except (socket.error, BrokenPipeError):
            self.update_log_signal.emit("發送失敗，伺服器可能已斷開。")
            self.stop_client()
        except Exception as e:
            self.update_log_signal.emit(f"發送時出錯: {e}")

    def stop_client(self):
        if self.is_connected:
            self.is_connected = False
            self.update_log_signal.emit("正在斷開連接...")
            if self.client_socket:
                try:
                    self.client_socket.shutdown(socket.SHUT_RDWR)
                    self.client_socket.close()
                except socket.error as e:
                    self.update_log_signal.emit(f"關閉連接時出錯: {e}")
                finally:
                    self.client_socket = None
            
            self.update_log_signal.emit("連接已斷開。")
            self.status_changed_signal.emit(False)
            self.disconnected_signal.emit()

class ClientWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt6 TCP 客戶端")
        self.setGeometry(200, 200, 500, 400)
        
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # --- 顶部：配置和控制 ---
        top_layout = QHBoxLayout()
        self.ip_input = QLineEdit("127.0.0.1")
        self.port_input = QLineEdit("8888")
        self.username_input = QLineEdit("用戶1")
        self.connect_button = QPushButton("連接")
        self.disconnect_button = QPushButton("斷開")
        self.status_label = QLabel("斷開")
        self.status_label.setStyleSheet("color: red; font-weight: bold;")
        self.disconnect_button.setEnabled(False)

        top_layout.addWidget(QLabel("伺服器 IP:"))
        top_layout.addWidget(self.ip_input)
        top_layout.addWidget(QLabel("端口:"))
        top_layout.addWidget(self.port_input)
        top_layout.addWidget(QLabel("用戶名:"))
        top_layout.addWidget(self.username_input)
        top_layout.addWidget(self.connect_button)
        top_layout.addWidget(self.disconnect_button)
        top_layout.addWidget(self.status_label)
        main_layout.addLayout(top_layout)

        # --- 中間：消息日誌 ---
        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        main_layout.addWidget(self.log_text_edit)

        # --- 底部：消息輸入和發送按鈕 ---
        bottom_layout = QHBoxLayout()
        self.recipient_select_box = QComboBox()
        self.message_input = QLineEdit()
        self.send_button = QPushButton("發送消息")
        self.send_file_button = QPushButton("發送文件")
        self.send_image_button = QPushButton("發送圖片")
        self.open_folder_button = QPushButton("打開接收文件目錄")

        self.recipient_select_box.addItem("所有用戶")
        self.recipient_select_box.setEnabled(False)
        self.message_input.setEnabled(False)
        self.send_button.setEnabled(False)
        self.send_file_button.setEnabled(False)
        self.send_image_button.setEnabled(False)
        self.open_folder_button.setEnabled(True)

        bottom_layout.addWidget(QLabel("發送給:"))
        bottom_layout.addWidget(self.recipient_select_box)
        bottom_layout.addWidget(self.message_input)
        bottom_layout.addWidget(self.send_button)
        bottom_layout.addWidget(self.send_file_button)
        bottom_layout.addWidget(self.send_image_button)
        bottom_layout.addWidget(self.open_folder_button)
        
        main_layout.addLayout(bottom_layout)

        # --- 信号連接 ---
        self.connect_button.clicked.connect(self.connect_clicked)
        self.disconnect_button.clicked.connect(self.disconnect_clicked)
        self.send_button.clicked.connect(self.send_message_clicked)
        self.send_file_button.clicked.connect(self.send_file_clicked)
        self.send_image_button.clicked.connect(self.send_image_clicked)
        self.open_folder_button.clicked.connect(self.open_received_folder)
        self.message_input.returnPressed.connect(self.send_message_clicked)

        self.client_thread = None
        self.client_worker = None
        
        self.destroyed.connect(self._cleanup_on_exit)

    def open_received_folder(self):
        folder_path = os.getcwd()
        try:
            if sys.platform == "win32":
                os.startfile(folder_path)
            elif sys.platform == "darwin":
                subprocess.run(["open", folder_path])
            else:
                subprocess.run(["xdg-open", folder_path])
        except Exception as e:
            QMessageBox.warning(self, "錯誤", f"無法打開文件目錄: {e}")
            
    def send_image_clicked(self):
        if not self.client_worker or not self.client_worker.is_connected:
            QMessageBox.warning(self, "錯誤", "未連接到伺服器，無法發送圖片。", QMessageBox.StandardButton.Ok)
            return

        recipient = self.recipient_select_box.currentText()
        if not self.check_recipient(recipient): return

        file_path, _ = QFileDialog.getOpenFileName(self, "選擇圖片文件", "", "圖片文件 (*.png *.jpg *.jpeg *.bmp *.gif)")
        if file_path:
            self.client_worker.send_file(recipient, file_path, "image")
            is_private = recipient != "所有用戶"
            self.update_log_with_image(file_path, "我", is_private)

    def connect_clicked(self):
        ip = self.ip_input.text()
        port_str = self.port_input.text()
        username = self.username_input.text().strip()
        
        if not username:
            QMessageBox.warning(self, "警告", "請輸入用戶名！", QMessageBox.StandardButton.Ok)
            return

        try:
            port = int(port_str)
            if not 1024 <= port <= 65535:
                raise ValueError("端口號超出有效範圍")

            self.client_thread = QThread()
            self.client_worker = ClientThread(ip, port, username)
            self.client_worker.moveToThread(self.client_thread)
            
            self.client_worker.update_log_signal.connect(self.update_log)
            self.client_worker.update_log_with_image_signal.connect(self.update_log_with_image)
            self.client_worker.status_changed_signal.connect(self.update_status)
            self.client_worker.connected_signal.connect(self.on_connected)
            self.client_worker.disconnected_signal.connect(self.on_disconnected)
            self.client_worker.online_users_updated.connect(self.update_online_users)
            self.client_thread.started.connect(self.client_worker.connect_to_server)
            
            self.ip_input.setEnabled(False)
            self.port_input.setEnabled(False)
            self.username_input.setEnabled(False)
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(True)
            self.message_input.setEnabled(True)
            self.send_button.setEnabled(True)
            self.send_file_button.setEnabled(True)
            self.send_image_button.setEnabled(True)
            self.recipient_select_box.setEnabled(True)
            
            self.client_thread.start()

        except ValueError as e:
            self.update_log(f"錯誤: 無效的端口號。{e}")
        except Exception as e:
            self.update_log(f"啟動客戶端時發生意外錯誤: {e}")

    def on_connected(self):
        self.send_button.setEnabled(True)
        self.message_input.setEnabled(True)
        self.send_file_button.setEnabled(True)
        self.send_image_button.setEnabled(True)

    def on_disconnected(self):
        self.send_button.setEnabled(False)
        self.message_input.setEnabled(False)
        self.send_file_button.setEnabled(False)
        self.send_image_button.setEnabled(False)

    def disconnect_clicked(self):
        if self.client_worker and self.client_worker.is_connected:
            self.client_worker.stop_client()
        
        if self.client_thread and self.client_thread.isRunning():
            self.client_thread.quit()
            self.client_thread.wait()

        self.client_thread = None
        self.client_worker = None
        
        self.ip_input.setEnabled(True)
        self.port_input.setEnabled(True)
        self.username_input.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)

        self.recipient_select_box.clear()
        self.recipient_select_box.addItem("所有用戶")
        self.recipient_select_box.setEnabled(False)

    def check_recipient(self, recipient):
        if recipient == "所有用戶" and (self.recipient_select_box.count() - 1) == 0:
            QMessageBox.information(self, "提示", "目前沒有其他在線用戶。", QMessageBox.StandardButton.Ok)
            return False
        return True

    def send_message_clicked(self):
        message = self.message_input.text()
        if not message:
            return
        
        if self.client_worker and self.client_worker.is_connected:
            recipient = self.recipient_select_box.currentText()
            is_private = recipient != "所有用戶"
            
            if not self.check_recipient(recipient): return
            
            self.client_worker.send_message(recipient, message)
            
            prefix = "[私密消息] " if is_private else ""
            self.update_log(f"{prefix}[我] {message}")
            self.message_input.clear()

    def send_file_clicked(self):
        if not self.client_worker or not self.client_worker.is_connected:
            QMessageBox.warning(self, "錯誤", "未連接到伺服器，無法發送文件。", QMessageBox.StandardButton.Ok)
            return

        recipient = self.recipient_select_box.currentText()
        if not self.check_recipient(recipient): return

        file_path, _ = QFileDialog.getOpenFileName(self, "選擇文件", "", "所有文件 (*)")
        if file_path:
            self.client_worker.send_file(recipient, file_path)
            is_private = recipient != "所有用戶"
            self.update_log(f"[{'私密文件' if is_private else '文件'}] [我] 發送: {os.path.basename(file_path)}")

    def update_log(self, message):
        current_datetime = QDateTime.currentDateTime()
        formatted_datetime = current_datetime.toString("yyyy-MM-dd hh:mm:ss")
        log_message = f"[{formatted_datetime}] {message}"
        self.log_text_edit.append(log_message)

    def update_log_with_image(self, image_path, sender, is_private):
        current_datetime = QDateTime.currentDateTime()
        formatted_datetime = current_datetime.toString("yyyy-MM-dd hh:mm:ss")
        
        display_sender = "我" if sender == self.client_worker.username else sender
        prefix = "[私密圖片] " if is_private else ""

        html_content = f"[{formatted_datetime}] **{prefix}{display_sender}:** <br><img src='file:///{image_path}' width='200'>"
        
        self.log_text_edit.append(html_content)
        self.log_text_edit.append("")

    def update_status(self, is_connected):
        if is_connected:
            self.status_label.setText("連接中")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.status_label.setText("斷開")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")

    def update_online_users(self, users):
        self.recipient_select_box.clear()
        self.recipient_select_box.addItem("所有用戶")
        
        for user in users:
            if user != self.client_worker.username:
                self.recipient_select_box.addItem(user)
    
    def _cleanup_on_exit(self):
        self.disconnect_clicked()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ClientWindow()
    window.show()
    sys.exit(app.exec())