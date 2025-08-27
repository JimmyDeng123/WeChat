import sys
import socket
import threading
import json
import os
import subprocess
from PyQt6.QtCore import QThread, QObject, pyqtSignal, QDateTime, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QTextEdit, QLabel, QComboBox, QMessageBox, QCheckBox,
    QFileDialog
)
from PyQt6.QtGui import QTextOption

# 定义文件接收状态
class ReceiverState:
    WAITING_FOR_HEADER = 0
    RECEIVING_FILE = 1

class ServerThread(QObject):
    update_log_signal = pyqtSignal(str)
    update_log_with_image_signal = pyqtSignal(str, str)
    status_changed_signal = pyqtSignal(bool)
    client_connected_signal = pyqtSignal(str)
    client_disconnected_signal = pyqtSignal(str)

    def __init__(self, ip, port, parent=None):
        super().__init__(parent)
        self.ip = ip
        self.port = port
        self.is_running = False
        self.server_socket = None
        self.client_sockets = {}
        self.client_states = {}
        self.client_usernames = {}
        self.username_to_id = {}

    def start_server(self):
        if self.is_running:
            return
        
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  
            self.server_socket.settimeout(1)
            self.server_socket.bind((self.ip, self.port))
            self.server_socket.listen(5)
            self.is_running = True
            
            self.status_changed_signal.emit(True)
            self.update_log_signal.emit(f"伺服器已啟動，監聽於 {self.ip}:{self.port}")
            
            self._accept_clients()
            
        except socket.error as e:
            self.update_log_signal.emit(f"啟動伺服器失敗: {e}")
            self.stop_server()
            
    def _accept_clients(self):
        while self.is_running:
            try:
                conn, addr = self.server_socket.accept()
                
                client_id = f"{addr[0]}:{addr[1]}"
                self.client_sockets[client_id] = conn
                self.client_states[client_id] = {
                    "state": ReceiverState.WAITING_FOR_HEADER,
                    "file_info": None,
                    "file_handle": None,
                    "received_bytes": 0,
                    "buffer": b""
                }
                
                self.client_connected_signal.emit(client_id)
                self.update_log_signal.emit(f"新客戶端連接: {client_id}")
                
                client_thread = threading.Thread(target=self._handle_client, args=(conn, client_id))
                client_thread.daemon = True
                client_thread.start()
                
            except socket.timeout:
                continue
            except Exception as e:
                self.update_log_signal.emit(f"接受連接時出錯: {e}")
                self.stop_server()
                break

    def _handle_client(self, conn, client_id):
        while self.is_running:
            try:
                data = conn.recv(4096)
                if not data:
                    break
                
                state_info = self.client_states.get(client_id)
                if not state_info:
                    break

                state_info["buffer"] += data

                if state_info["state"] == ReceiverState.WAITING_FOR_HEADER:
                    try:
                        # 嘗試尋找 JSON 數據結束的標記
                        message_str = state_info["buffer"].decode("utf-8")
                        header_end_index = message_str.find('}')
                        if header_end_index != -1:
                            # 找到了完整的 JSON 數據
                            header_json_str = message_str[:header_end_index + 1]
                            header = json.loads(header_json_str)
                            
                            # 更新緩衝區，移除已處理的 JSON 數據
                            remaining_data = state_info["buffer"][len(header_json_str.encode("utf-8")):]
                            state_info["buffer"] = remaining_data

                            # 處理 JSON 協議消息
                            self._handle_json_message(header, client_id, remaining_data)
                        
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # 可能是二進位數據，跳過解析
                        pass

                elif state_info["state"] == ReceiverState.RECEIVING_FILE:
                    self._handle_file_data(state_info["buffer"], client_id)
                    state_info["buffer"] = b""
            
            except (socket.error, ConnectionResetError):
                break
        
        username = self.client_usernames.get(client_id, "未知")
        self.update_log_signal.emit(f"客戶端 [{username}] ({client_id}) 已斷開連接")
        
        if client_id in self.client_sockets:
            del self.client_sockets[client_id]
        if client_id in self.client_states:
            del self.client_states[client_id]
        if client_id in self.client_usernames:
            del self.client_usernames[client_id]
        if username in self.username_to_id:
            del self.username_to_id[username]

        leave_message = f"成員 [{username}] 離開了聊天室。"
        self.update_log_signal.emit(leave_message)
        self.broadcast_message(leave_message, sender="伺服器")
        self.broadcast_online_users()

        self.client_disconnected_signal.emit(client_id)
        conn.close()

    def _handle_json_message(self, header, client_id, remaining_data):
        protocol_type = header.get("type")
        
        if protocol_type == "join" and "username" in header:
            username = header["username"]
            self.client_usernames[client_id] = username
            self.username_to_id[username] = client_id
            self.update_log_signal.emit(f"客戶端 {client_id} 已設置用戶名為: {username}")
            
            join_message = f"成員 [{username}] 加入了聊天室。"
            self.update_log_signal.emit(join_message)
            self.broadcast_message(join_message, sender="伺服器")
            self.broadcast_online_users()

        elif protocol_type == "message" and "sender" in header and "content" in header and "recipient" in header:
            sender = header["sender"]
            content = header["content"]
            recipient = header["recipient"]
            
            self.update_log_signal.emit(f"收到來自 [{sender}] 的消息，接收者為: {recipient}")
            
            if recipient == "所有用戶":
                self.broadcast_message(content, sender=sender)
            else:
                self.send_private_message(sender, recipient, content)

        elif protocol_type == "file_start" and "filename" in header and "size" in header and "sender" in header and "recipient" in header:
            sender = header["sender"]
            recipient = header["recipient"]
            state_info = self.client_states.get(client_id)
            
            self.update_log_signal.emit(f"準備從 [{sender}] 接收文件，接收者為: {recipient}")
            
            save_path = os.path.join(os.getcwd(), f"server_received_{os.path.basename(header['filename'])}")
            state_info["file_handle"] = open(save_path, "wb")
            state_info["file_info"] = header
            state_info["received_bytes"] = 0
            state_info["state"] = ReceiverState.RECEIVING_FILE
            
            if recipient == "所有用戶":
                self.broadcast_file_header(header)
            else:
                self.send_private_file_header(header)
            
            # 立即處理剩餘的數據
            if remaining_data:
                self._handle_file_data(remaining_data, client_id)

        else:
            self.update_log_signal.emit(f"收到未知 JSON 消息: {header}")


    def _handle_file_data(self, data, client_id):
        state_info = self.client_states.get(client_id)
        if not state_info:
            return
        
        state_info["file_handle"].write(data)
        state_info["received_bytes"] += len(data)
            
        file_info = state_info["file_info"]
        recipient = file_info.get("recipient", "所有用戶")
        
        if recipient == "所有用戶":
            self.broadcast_file_data(data, file_info)
        else:
            self.send_private_file_data(data, file_info)
            
        if state_info["received_bytes"] >= state_info["file_info"]["size"]:
            state_info["file_handle"].close()
            
            filetype = state_info['file_info'].get('filetype', "file")
            filename = state_info['file_info']['filename']
            sender = state_info['file_info'].get('sender', "未知")
            
            if filetype == "image":
                save_path = os.path.join(os.getcwd(), f"server_received_{os.path.basename(filename)}")
                self.update_log_with_image_signal.emit(save_path, sender)
            else:
                self.update_log_signal.emit(f"文件接收完成: {filename}")
            
            state_info["state"] = ReceiverState.WAITING_FOR_HEADER
            state_info["file_info"] = None
            state_info["file_handle"] = None
            
    def stop_server(self):
        if self.is_running:
            self.is_running = False
            self.update_log_signal.emit("正在關閉伺服器...")
            
            for client_id, client_socket in list(self.client_sockets.items()):
                try:
                    client_socket.close()
                    self.client_disconnected_signal.emit(client_id)
                except:
                    pass
            self.client_sockets.clear()
            self.client_states.clear()
            self.client_usernames.clear()
            self.username_to_id.clear()
            
            if self.server_socket:
                self.server_socket.close()
                self.server_socket = None
            
            self.update_log_signal.emit("伺服器已停止。")
            self.status_changed_signal.emit(False)
            
    def broadcast_online_users(self):
        users_list = list(self.username_to_id.keys())
        msg_obj = {"type": "online_users", "users": users_list}
        msg_bytes = json.dumps(msg_obj).encode("utf-8")
        
        clients_to_remove = []
        for client_socket in self.client_sockets.values():
            try:
                client_socket.sendall(msg_bytes)
            except (socket.error, BrokenPipeError):
                pass
        
    def broadcast_message(self, content, sender="伺服器"):
        msg_obj = {"type": "message", "sender": sender, "content": content}
        msg_bytes = json.dumps(msg_obj).encode("utf-8")
        
        for client_socket in self.client_sockets.values():
            try:
                client_socket.sendall(msg_bytes)
            except (socket.error, BrokenPipeError):
                pass
    
    def send_private_message(self, sender, recipient, content):
        recipient_id = self.username_to_id.get(recipient)
        if not recipient_id or recipient_id not in self.client_sockets:
            self.update_log_signal.emit(f"私密消息發送失敗: 用戶 [{recipient}] 不在線。")
            return

        msg_obj = {"type": "message", "sender": sender, "content": content, "is_private": True}
        msg_bytes = json.dumps(msg_obj).encode("utf-8")
        
        try:
            self.client_sockets[recipient_id].sendall(msg_bytes)
        except (socket.error, BrokenPipeError):
            self.update_log_signal.emit(f"私密消息發送給 [{recipient}] 失敗，對方可能已斷開。")

    def broadcast_file_header(self, header):
        clients_to_remove = []
        header_bytes = json.dumps(header).encode("utf-8")
        for client_socket in self.client_sockets.values():
            try:
                client_socket.sendall(header_bytes)
            except (socket.error, BrokenPipeError):
                pass

    def send_private_file_header(self, header):
        recipient = header["recipient"]
        recipient_id = self.username_to_id.get(recipient)
        if not recipient_id or recipient_id not in self.client_sockets:
            self.update_log_signal.emit(f"私密文件頭發送失敗: 用戶 [{recipient}] 不在線。")
            return
            
        header["is_private"] = True
        header_bytes = json.dumps(header).encode("utf-8")
        
        try:
            self.client_sockets[recipient_id].sendall(header_bytes)
        except (socket.error, BrokenPipeError):
            self.update_log_signal.emit(f"私密文件頭發送給 [{recipient}] 失敗，對方可能已斷開。")

    def broadcast_file_data(self, data, file_info):
        for client_socket in self.client_sockets.values():
            try:
                client_socket.sendall(data)
            except (socket.error, BrokenPipeError):
                pass

    def send_private_file_data(self, data, file_info):
        recipient = file_info.get("recipient")
        recipient_id = self.username_to_id.get(recipient)
        if not recipient_id or recipient_id not in self.client_sockets:
            return
        
        try:
            self.client_sockets[recipient_id].sendall(data)
        except (socket.error, BrokenPipeError):
            pass

class ServerWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt6 TCP 伺服器")
        self.setGeometry(100, 100, 600, 500)
        
        self.log_filename = "tcp.log"

        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        top_layout = QHBoxLayout()
        self.ip_input = QLineEdit("127.0.0.1")
        self.port_input = QLineEdit("8888")
        self.start_button = QPushButton("啟動")
        self.stop_button = QPushButton("停止")
        self.clear_button = QPushButton("清空日誌")
        self.status_label = QLabel("停止")
        self.status_label.setStyleSheet("color: red; font-weight: bold;")
        self.stop_button.setEnabled(False)

        top_layout.addWidget(QLabel("IP:"))
        top_layout.addWidget(self.ip_input)
        top_layout.addWidget(QLabel("端口:"))
        top_layout.addWidget(self.port_input)
        top_layout.addWidget(self.start_button)
        top_layout.addWidget(self.stop_button)
        top_layout.addWidget(self.clear_button)
        top_layout.addWidget(self.status_label)
        main_layout.addLayout(top_layout)

        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        main_layout.addWidget(self.log_text_edit)

        bottom_layout = QHBoxLayout()
        self.open_folder_button = QPushButton("打開接收文件目錄")
        bottom_layout.addWidget(self.open_folder_button)
        main_layout.addLayout(bottom_layout)

        self.start_button.clicked.connect(self.start_server_clicked)
        self.stop_button.clicked.connect(self.stop_server_clicked)
        self.clear_button.clicked.connect(self.clear_log)
        self.open_folder_button.clicked.connect(self.open_received_folder)

        self.server_thread = None
        self.server_worker = None
        
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

    def start_server_clicked(self):
        ip = self.ip_input.text()
        port_str = self.port_input.text()
        
        try:
            port = int(port_str)
            if not 1024 <= port <= 65535:
                raise ValueError("端口號超出有效範圍")

            self.server_thread = QThread()
            self.server_worker = ServerThread(ip, port)
            self.server_worker.moveToThread(self.server_thread)
            
            self.server_worker.update_log_signal.connect(self.update_log)
            self.server_worker.update_log_with_image_signal.connect(self.update_log_with_image)
            self.server_worker.status_changed_signal.connect(self.update_status)
            self.server_worker.client_connected_signal.connect(self._add_client_to_list)
            self.server_worker.client_disconnected_signal.connect(self._remove_client_from_list)
            self.server_thread.started.connect(self.server_worker.start_server)

            self.ip_input.setEnabled(False)
            self.port_input.setEnabled(False)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            
            self.server_thread.start()

        except ValueError as e:
            self.update_log(f"錯誤: 無效的端口號。{e}")
        except Exception as e:
            self.update_log(f"啟動伺服器時發生意外錯誤: {e}")

    def stop_server_clicked(self):
        if self.server_worker and self.server_worker.is_running:
            self.server_worker.stop_server()
        
        if self.server_thread and self.server_thread.isRunning():
            self.server_thread.quit()
            self.server_thread.wait()

        self.server_thread = None
        self.server_worker = None
        
        self.ip_input.setEnabled(True)
        self.port_input.setEnabled(True)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _add_client_to_list(self, client_id):
        pass

    def _remove_client_from_list(self, client_id):
        pass

    def _cleanup_on_exit(self):
        self.stop_server_clicked()

    def update_log(self, message):
        current_datetime = QDateTime.currentDateTime()
        formatted_datetime = current_datetime.toString("yyyy-MM-dd hh:mm:ss")
        
        log_message = f"[{formatted_datetime}] {message}"
        
        try:
            with open(self.log_filename, "a", encoding="utf-8") as f:
                f.write(log_message + "\n")
        except Exception as e:
            self.log_text_edit.append(f"寫入日誌文件失敗: {e}")
            
        self.log_text_edit.append(log_message)

    def update_log_with_image(self, image_path, sender):
        current_datetime = QDateTime.currentDateTime()
        formatted_datetime = current_datetime.toString("yyyy-MM-dd hh:mm:ss")
        
        display_sender = f"客戶端({sender})"
        html_content = f"[{formatted_datetime}] **{display_sender}:** <br><img src='file:///{image_path}' width='200'>"
        
        self.log_text_edit.append(html_content)
        self.log_text_edit.append("")

    def clear_log(self):
        self.log_text_edit.clear()

    def update_status(self, is_running):
        if is_running:
            self.status_label.setText("運行中")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.status_label.setText("停止")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ServerWindow()
    window.show()
    sys.exit(app.exec())