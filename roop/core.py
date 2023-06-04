import platform, signal, sys, shutil, glob, argparse, os, webbrowser, psutil, cv2, threading
import torch
import tkinter as tk
import multiprocessing as mp
from tkinter import filedialog
from opennsfw2 import predict_video_frames, predict_image
from tkinter.filedialog import asksaveasfilename
from pathlib import Path
from datetime import datetime
from PIL import Image, ImageTk

import roop.globals
from roop.swapper import process_video, process_img
from roop.utils import is_img, detect_fps, set_fps, create_video, add_audio, extract_frames, rreplace
from roop.analyser import get_face_single

run_at = datetime.now()

if 'ROCMExecutionProvider' in roop.globals.providers:
    del torch

pool = None
args = {}

signal.signal(signal.SIGINT, lambda signal_number, frame: quit())
parser = argparse.ArgumentParser()
parser.add_argument('-f', '--face', help='use this face', dest='source_img')
parser.add_argument('-t', '--target', help='replace this face', dest='target_path')
parser.add_argument('-o', '--output', help='save output to this file', dest='output_file')
parser.add_argument('--gpu', help='use gpu', dest='gpu', action='store_true', default=False)
parser.add_argument('--keep-fps', help='maintain original fps', dest='keep_fps', action='store_true', default=False)
parser.add_argument('--keep-frames', help='keep frames directory', dest='keep_frames', action='store_true', default=False)
parser.add_argument('--max-memory', help='maximum amount of RAM in GB to be used', type=int)
parser.add_argument('--max-cores', help='number of cores to be use for CPU mode', dest='cores_count', type=int, default=max(psutil.cpu_count() - 2, 2))
parser.add_argument('--all-faces', help='swap all faces in frame', dest='all_faces', action='store_true', default=False)

for name, value in vars(parser.parse_args()).items():
    args[name] = value

if '--all-faces' in sys.argv or '-a' in sys.argv:
    roop.globals.all_faces = True

sep = "/"
if os.name == "nt":
    sep = "\\"


def limit_resources():
    if args['max_memory']:
        memory = args['max_memory'] * 1024 * 1024 * 1024
        if str(platform.system()).lower() == 'windows':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSize(-1, ctypes.c_size_t(memory), ctypes.c_size_t(memory))
        else:
            import resource
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))


def pre_check():
    if sys.version_info < (3, 9):
        quit('Python version is not supported - please upgrade to 3.9 or higher')
    if not shutil.which('ffmpeg'):
        quit('ffmpeg is not installed!')
    model_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), '../inswapper_128.onnx')
    if not os.path.isfile(model_path):
        quit('File "inswapper_128.onnx" does not exist!')
    if '--gpu' not in sys.argv:
        roop.globals.providers = ['CPUExecutionProvider']
    if '--all-faces' in sys.argv or '-a' in sys.argv:
        roop.globals.all_faces = True


def start_processing():
    frame_paths = args["frame_paths"]
    n = len(frame_paths) // (args['cores_count'])
    # single thread
    if args['gpu'] or n < 2:
        process_video(args['source_img'], args["frame_paths"])
        return
    # multithread if total frames to cpu cores ratio is greater than 2
    if n > 2:
        processes = []
        for i in range(0, len(frame_paths), n):
            p = pool.apply_async(process_video, args=(args['source_img'], frame_paths[i:i+n],))
            processes.append(p)
        for p in processes:
            p.get()
        pool.close()
        pool.join()


def preview_image(image_path):
    img = Image.open(image_path)
    img = img.resize((192, 250), Image.ANTIALIAS)
    photo_img = ImageTk.PhotoImage(img)
    left_img_label.configure(image=photo_img)
    left_img_label.image = photo_img
    img.close()


def preview_video(video_path):
    img = None
    if not is_img(video_path):
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print("Error opening video file")
            return
        ret, frame = cap.read()
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame)
        cap.release()
    else:
        img = Image.open(video_path)
    img = img.resize((192, 250), Image.ANTIALIAS)
    photo_img = ImageTk.PhotoImage(img)
    right_img_label.configure(image=photo_img)
    right_img_label.image = photo_img
    img.close()


def select_face():
    args['source_img'] = filedialog.askopenfilename(title="Select a face")
    preview_image(args['source_img'])


def select_target():
    args['target_path'] = filedialog.askopenfilename(title="Select a target")
    threading.Thread(target=preview_video, args=(args['target_path'],)).start()


def toggle_fps_limit():
    args['keep_fps'] = int(limit_fps.get() != True)


def toggle_all_faces():
    roop.globals.all_faces = True if all_faces.get() == 1 else False


def toggle_keep_frames():
    args['keep_frames'] = int(keep_frames.get())


def save_file():
    filename, ext = 'output.mp4', '.mp4'
    if is_img(args['target_path']):
        filename, ext = 'output.png', '.png'
    args['output_file'] = asksaveasfilename(initialfile=filename, defaultextension=ext, filetypes=[("All Files","*.*"),("Videos","*.mp4")])


def status(string):
    if 'cli_mode' in args:
        print("Status: " + string)
    else:
        status_label["text"] = "Status: " + string
        window.update()


def start():
    enable_button(False)
    if not args['source_img'] or not os.path.isfile(args['source_img']):
        print("\n[WARNING] Please select an image containing a face.")
        return
    elif not args['target_path'] or not os.path.isfile(args['target_path']):
        print("\n[WARNING] Please select a video/image to swap face in.")
        return
    if not args['output_file']:
        target_path = args['target_path']
        args['output_file'] = rreplace(target_path, "/", "/swapped-", 1) if "/" in target_path else "swapped-" + target_path
    global pool
    pool = mp.Pool(args['cores_count'])
    target_path = args['target_path']
    test_face = get_face_single(cv2.imread(args['source_img']))
    if not test_face:
        print("\n[WARNING] No face detected in source image. Please try with another one.\n")
        return
    if is_img(target_path):
        if predict_image(target_path) > 0.85:
            quit()
        process_img(args['source_img'], target_path, args['output_file'])
        status("swap successful!")
        enable_button(True)
        return
    seconds, probabilities = predict_video_frames(video_path=args['target_path'], frame_interval=100)
    if any(probability > 0.85 for probability in probabilities):
        quit()
    video_name_full = target_path.split("/")[-1]
    video_name = os.path.splitext(video_name_full)[0]
    output_dir = os.path.dirname(target_path) + "/" + video_name
    Path(output_dir).mkdir(exist_ok=True)
    status("detecting video's FPS...")
    fps, exact_fps = detect_fps(target_path)
    if not args['keep_fps'] and fps > 30:
        this_path = output_dir + "/" + video_name + ".mp4"
        set_fps(target_path, this_path, 30)
        target_path, exact_fps = this_path, 30
    else:
        shutil.copy(target_path, output_dir)
    status("extracting frames...")
    extract_frames(target_path, output_dir)
    args['frame_paths'] = tuple(sorted(
        glob.glob(output_dir + "/*.png"),
        key=lambda x: int(x.split(sep)[-1].replace(".png", ""))
    ))
    status("swapping in progress...")
    start_processing()
    status("creating video...")
    create_video(video_name, exact_fps, output_dir)
    status("adding audio...")
    add_audio(output_dir, target_path, video_name_full, args['keep_frames'], args['output_file'])
    save_path = args['output_file'] if args['output_file'] else output_dir + "/" + video_name + ".mp4"
    print("\n\nVideo saved as:", save_path, "\n\n")
    status("swap successful!")
    enable_button(True)

def enable_button(state):
    if state:
        face_button["state"] = "normal"
        target_button["state"] = "normal"
        start_button["state"] = "normal"
        all_faces_checkbox["state"] = "normal"
        fps_checkbox["state"] = "normal"
        frames_checkbox["state"] = "normal"
    else:
        face_button["state"] = "disabled"
        target_button["state"] = "disabled"
        start_button["state"] = "disabled"
        all_faces_checkbox["state"] = "disabled"
        fps_checkbox["state"] = "disabled"
        frames_checkbox["state"] = "disabled"

def run():
    global all_faces, keep_frames, limit_fps, status_label, window, face_button, target_button, start_button
    global all_faces_checkbox, fps_checkbox, frames_checkbox, left_img_label, right_img_label


    pre_check()
    limit_resources()

    if args['source_img']:
        args['cli_mode'] = True
        start()
        quit()
    window = tk.Tk()
    window.geometry("540x368")
    window.title("roop")
    window.configure(bg="#2d3436")
    window.resizable(width=False, height=False)

    # Load image placeholder
    img = Image.open("./nopreview.jpg")
    img = img.resize((192, 250), Image.ANTIALIAS)
    photo_img = ImageTk.PhotoImage(img)

    # Contact information
    support_link = tk.Label(window, text="Donate to project <3", fg="#fd79a8", bg="#2d3436", cursor="hand2", font=("Arial", 8))
    support_link.place(x=410,y=20,width=118,height=30)
    support_link.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/sponsors/s0md3v"))

    # Face Image Window
    left_frame = tk.Frame(window)
    left_frame.place(x=10, y=30)
    left_img_label = tk.Label(left_frame, image=photo_img)
    left_img_label.pack()

    # Target Image Window
    right_frame = tk.Frame(window)
    right_frame.place(x=210, y=30)
    right_img_label = tk.Label(right_frame, image=photo_img)
    right_img_label.pack()

    img.close()

    # Select a face button
    face_button = tk.Button(window, text="Select a face", command=select_face, bg="#f1c40f", highlightthickness=4, relief="flat", highlightbackground="#74b9ff", activebackground="#74b9ff", borderwidth=4)
    face_button.place(x=10,y=290,width=192,height=30)

    # Select a target button
    target_button = tk.Button(window, text="Select a target", command=select_target, bg="#f1c40f", highlightthickness=4, relief="flat", highlightbackground="#74b9ff", activebackground="#74b9ff", borderwidth=4)
    target_button.place(x=210,y=290,width=192,height=30)

    # All faces checkbox
    all_faces = tk.IntVar()
    all_faces_checkbox = tk.Checkbutton(window, anchor="w", relief="groove", activebackground="#2d3436", activeforeground="#74b9ff", selectcolor="black", text="Process all faces in frame", fg="#dfe6e9", borderwidth=0, highlightthickness=0, bg="#2d3436", variable=all_faces, command=toggle_all_faces)
    all_faces_checkbox.place(x=410,y=200,width=120,height=25)

    # FPS limit checkbox
    limit_fps = tk.IntVar(None, not args['keep_fps'])
    fps_checkbox = tk.Checkbutton(window, anchor="w", relief="groove", activebackground="#2d3436", activeforeground="#74b9ff", selectcolor="black", text="Limit FPS to 30", fg="#dfe6e9", borderwidth=0, highlightthickness=0, bg="#2d3436", variable=limit_fps, command=toggle_fps_limit)
    fps_checkbox.place(x=410,y=220,width=120,height=25)

    # Keep frames checkbox
    keep_frames = tk.IntVar(None, args['keep_frames'])
    frames_checkbox = tk.Checkbutton(window, anchor="w", relief="groove", activebackground="#2d3436", activeforeground="#74b9ff", selectcolor="black", text="Keep frames dir", fg="#dfe6e9", borderwidth=0, highlightthickness=0, bg="#2d3436", variable=keep_frames, command=toggle_keep_frames)
    frames_checkbox.place(x=410,y=240,width=120,height=25)

    # Start button
    start_button = tk.Button(window, text="Start", bg="#f1c40f", relief="flat", borderwidth=0, highlightthickness=0, command=lambda: [save_file(), threading.Thread(target=start).start()])
    start_button.place(x=410,y=270,width=120,height=50)

    # Status label
    startup_time = int((datetime.now()-run_at).total_seconds() * 1000)
    status_label = tk.Label(window, justify="center", text=f"Status: waiting for input...\nStartup time {startup_time}ms", fg="#2ecc71", bg="#2d3436")
    status_label.place(x=0,y=320,width=540,height=30)

    window.mainloop()
