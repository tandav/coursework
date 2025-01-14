from pyqtgraph.Qt import QtCore, QtGui
from PyQt5.QtGui import QApplication
from scipy.fftpack import fft
from scipy.io.wavfile import write as write_wav
from scipy.io import wavfile
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib
matplotlib.style.use('classic')
import pyqtgraph as pg
import numpy as np
import time
import threading
import sys
import serial # TODO: try del
import serial.tools.list_ports
import socket
import signal
import os
import gzip
import shutil


class SerialReader(threading.Thread):
    """ Defines a thread for reading and buffering serial data.
    By default, about 5MSamples are stored in the buffer.
    Data can be retrieved from the buffer by calling get(N)"""
    def __init__(self, data_collected_signal, chunkSize=1024, chunks=5000):
        threading.Thread.__init__(self)
        # circular buffer for storing serial data until it is
        # fetched by the GUI
        self.buffer = np.zeros(chunks*chunkSize, dtype=np.uint16)
        self.chunks = chunks        # number of chunks to store in the buffer
        self.chunkSize = chunkSize  # size of a single chunk (items, not bytes)
        self.ptr = 0                # pointer to most (recently collected buffer index) + 1
        # self.port = port            # serial port handle
        self.port = self.find_device_and_return_port()           # serial port handle
        self.sps = 0.0              # holds the average sample acquisition rate
        self.exitFlag = False
        self.exitMutex = threading.Lock()
        self.dataMutex = threading.Lock()
        self.values_recorded = 0
        self.data_collected_signal = data_collected_signal

    def find_device_and_return_port(self):
        for i in range(61):
            ports = list(serial.tools.list_ports.comports())
            for port in ports:
                if 'Arduino' in port.description or \
                   'Устройство с последовательным интерфейсом USB' in port.description or \
                   'USB Serial Device' in port.description: 
                # if ('Устройство с последовательным интерфейсом USB') in port.description: 
                    # try / except
                    ser = serial.Serial(port.device)
                    print('device connected')
                    break
            else:
                if i == 60:
                    print('\nDevice not found. Check the connection.')
                    sys.exit()
                sys.stdout.write('\rsearching device' + '.'*i + ' ')
                sys.stdout.flush()
                time.sleep(0.05)
                continue  # executed if the loop ended normally (no break)
            break  # executed if 'continue' was skipped (break)
        return ser
    def run(self):
        exitMutex = self.exitMutex
        dataMutex = self.dataMutex
        buffer = self.buffer
        port = self.port
        count = 0
        sps = None
        lastUpdate = time.time()
        # lastUpdate = pg.ptime.time()
        ptr2 = 0

        global record_buffer, recording, values_to_record, t2, record_end_time, NFFT, gui, overlap

        while True:
            # see whether an exit was requested
            with exitMutex:
                if self.exitFlag:
                    port.close()
                    break

            # read one full chunk from the serial port
            data = port.read(self.chunkSize*2) # *2 probably because of datatypes/bytes/things like that
            # convert data to 16bit int numpy array TODO: convert here to -1..+1 values, instead voltage 0..3.3
            data = np.fromstring(data, dtype=np.uint16)

            # keep track of the acquisition rate in samples-per-second
            count += self.chunkSize
            # now = pg.ptime.time()
            now = time.time()

            dt = now-lastUpdate
            if dt > 1.0:
                # sps is an exponential average of the running sample rate measurement
                if sps is None:
                    sps = count / dt
                else:
                    sps = sps * 0.9 + (count / dt) * 0.1
                count = 0
                lastUpdate = now

            # write the new chunk into the circular buffer
            # and update the buffer pointer
            with dataMutex:
                buffer[self.ptr:self.ptr+self.chunkSize] = data
                self.ptr = (self.ptr + self.chunkSize) % buffer.shape[0]
                ptr2 += self.chunkSize

                if sps is not None:
                    self.sps = sps

                if recording:
                    record_buffer[self.values_recorded : self.values_recorded + self.chunkSize] = data
                    self.values_recorded += self.chunkSize

                    if self.values_recorded >= values_to_record: # maybe del second condition
                        record_end_time = time.time()
                        recording = False
                        self.values_recorded = 0
                        values_to_record = 0
                        t2 = threading.Thread(target=send_to_cuda)
                        t2.start()
                
                elif ptr2 >= NFFT - overlap:
                    ptr2 = 0
                    self.data_collected_signal.emit()

    def get(self, num):
        """ Return a tuple (time_values, voltage_values, rate)
          - voltage_values will contain the *num* most recently-collected samples
            as a 32bit float array.
          - time_values assumes samples are collected at 1MS/s
          - rate is the running average sample rate.
        """
        with self.dataMutex:  # lock the buffer and copy the requested data out
            ptr = self.ptr
            if ptr-num < 0:
                data = np.empty(num, dtype=np.uint16)
                data[:num-ptr] = self.buffer[ptr-num:] # last N=ptr values of the buffer
                data[num-ptr:] = self.buffer[:ptr]
            else:
                data = self.buffer[self.ptr-num:self.ptr].copy()
            rate = self.sps

        # Convert array to float and rescale to voltage.
        # Assume 3.3V / 12bits
        # (we need calibration data to do a better job on this)
        data = data.astype(np.float32) * (3.3 / 2**12) * 2 / 3.3 - 1
        return np.linspace(0, (num-1)*1e-6, num), data, rate

    def exit(self):
        """ Instruct the serial thread to exit."""
        with self.exitMutex:
            self.exitFlag = True



class AppGUI(QtGui.QWidget):
    data_collected = QtCore.pyqtSignal()
    chunk_recorded = QtCore.pyqtSignal()

    def __init__(self, plot_points_x, plot_points_y=256):
        super(AppGUI, self).__init__()
        # global NFFT
        
        self.rate = 1

        self.plot_points_y = plot_points_y
        self.plot_points_x = plot_points_x
        self.img_array = np.zeros((self.plot_points_x, self.plot_points_y)) # rename to (plot_width, plot_height)

        self.init_ui()
        self.qt_connections()
        
        self.t = np.linspace(0, (NFFT - 1) * 1e-6, NFFT)
        self.y = np.zeros(NFFT)
        self.f = np.zeros(NFFT // 2)
        self.a = np.zeros(NFFT // 2)
        self.win = np.hanning(NFFT)

        self.avg_sum = 0
        self.avg_iters = 0

    def init_ui(self):
        global record_name, NFFT, chunkSize, overlap
        pg.setConfigOption('background', 'w')
        pg.setConfigOption('foreground', 'k')

        self.setWindowTitle('Signal from stethoscope')
        self.layout = QtGui.QVBoxLayout()

        self.fft_slider_box = QtGui.QHBoxLayout()
        self.fft_chunks_slider = QtGui.QSlider()
        self.fft_chunks_slider.setOrientation(QtCore.Qt.Horizontal)
        self.fft_chunks_slider.setRange(10, 20) # max is ser_reader_thread.chunks
        self.fft_chunks_slider.setValue(18)
        # self.fft_chunks_slider.setValue(15)
        NFFT = 2 ** self.fft_chunks_slider.value()
        self.fft_chunks_slider.setTickPosition(QtGui.QSlider.TicksBelow)
        self.fft_chunks_slider.setTickInterval(1)
        self.fft_slider_label = QtGui.QLabel('FFT window: {}'.format(NFFT))
        self.fft_slider_box.addWidget(self.fft_slider_label)
        self.fft_slider_box.addWidget(self.fft_chunks_slider)
        self.layout.addLayout(self.fft_slider_box)

        self.overlap_slider_box = QtGui.QHBoxLayout()
        self.overlap_slider = QtGui.QSlider()
        self.overlap_slider.setOrientation(QtCore.Qt.Horizontal)
        self.overlap_slider.setRange(0, NFFT - 1) # max is ser_reader_thread.chunks
        # overlap = NFFT // 2
        overlap = NFFT * 0.85
        self.overlap_slider.setValue(overlap)
        # self.fft_chunks_slider.setValue(128)
        # overlap = self.overlap_slider.value()
        # self.overlap_slider.setTickPosition(QtGui.QSlider.TicksBelow) # too many ticks
        self.overlap_slider.setTickInterval(1)
        self.overlap_slider_label = QtGui.QLabel('FFT window overlap: {}'.format(overlap))
        self.overlap_slider_box.addWidget(self.overlap_slider_label)
        self.overlap_slider_box.addWidget(self.overlap_slider)
        self.layout.addLayout(self.overlap_slider_box)

        self.plot_points_x_slider_box = QtGui.QHBoxLayout()
        self.plot_points_x_slider = QtGui.QSlider()
        self.plot_points_x_slider.setOrientation(QtCore.Qt.Horizontal)
        self.plot_points_x_slider.setRange(16, 8192) # max is ser_reader_thread.chunks
        self.plot_points_x_slider.setValue(256)
        self.plot_points_x = self.plot_points_x_slider.value()
        self.fft_chunks_slider.setTickPosition(QtGui.QSlider.TicksBelow)
        self.plot_points_x_slider.setTickInterval(16)
        self.plot_points_x_slider_label = QtGui.QLabel('plot_points_x: {}'.format(self.plot_points_x))
        self.plot_points_x_slider_box.addWidget(self.plot_points_x_slider_label)
        self.plot_points_x_slider_box.addWidget(self.plot_points_x_slider)
        self.layout.addLayout(self.plot_points_x_slider_box)

        self.plot_points_y_slider_box = QtGui.QHBoxLayout()
        self.plot_points_y_slider = QtGui.QSlider()
        self.plot_points_y_slider.setOrientation(QtCore.Qt.Horizontal)
        self.plot_points_y_slider.setRange(16, 8192) # max is ser_reader_thread.chunks
        self.plot_points_y_slider.setValue(256)
        self.plot_points_y = self.plot_points_y_slider.value()
        self.fft_chunks_slider.setTickPosition(QtGui.QSlider.TicksBelow)
        self.plot_points_y_slider.setTickInterval(16)
        self.plot_points_y_slider_label = QtGui.QLabel('plot_points_y: {}'.format(self.plot_points_y))
        self.plot_points_y_slider_box.addWidget(self.plot_points_y_slider_label)
        self.plot_points_y_slider_box.addWidget(self.plot_points_y_slider)
        self.layout.addLayout(self.plot_points_y_slider_box)

        self.make_plots_box = QtGui.QHBoxLayout()
        self.signal_checkbox      = QtGui.QCheckBox('Signal')
        self.fft_checkbox         = QtGui.QCheckBox('FFT')
        self.spectrogram_checkbox = QtGui.QCheckBox('Spectrogram')
        self.wavelet_checkbox     = QtGui.QCheckBox('Wavelet')
        self.signal_checkbox     .toggle()
        self.fft_checkbox        .toggle()
        self.spectrogram_checkbox.toggle()
        self.wavelet_checkbox    .toggle()


        self.make_plots_box.addWidget(self.signal_checkbox)
        self.make_plots_box.addWidget(self.fft_checkbox)
        self.make_plots_box.addWidget(self.spectrogram_checkbox)
        self.make_plots_box.addWidget(self.wavelet_checkbox)


        self.make_plots_button = QtGui.QPushButton('Make Plots')
        self.make_plots_box.addWidget(self.make_plots_button)

        self.layout.addLayout(self.make_plots_box)


        # self.plot_points_y_slider_label = QtGui.QLabel('plot_points_y: {}'.format(self.plot_points_y))
        # self.make_plots_box.addWidget(self.plot_points_y_slider_label)
        # self.make_plots_box.addWidget(self.plot_points_y_slider)
        # self.layout.addLayout(self.make_plots_box)

        self.signal_widget = pg.PlotWidget()
        self.signal_widget.showGrid(x=True, y=True, alpha=0.1)
        self.signal_widget.setYRange(-1, 1)
        self.signal_curve = self.signal_widget.plot(pen='b')

        self.fft_widget = pg.PlotWidget(title='FFT')
        self.fft_widget.showGrid(x=True, y=True, alpha=0.1)
        self.fft_widget.setLogMode(x=True, y=False)
        # self.fft_widget.setLogMode(x=False, y=False)
        # self.fft_widget.setYRange(0, 0.1) # w\o np.log(a)
        # self.fft_widget.setYRange(-15, 0) # w/ np.log(a)
        self.fft_curve = self.fft_widget.plot(pen='r')

        self.layout.addWidget(self.signal_widget)
        self.layout.addWidget(self.fft_widget)

        self.record_box = QtGui.QHBoxLayout()
        self.spin = pg.SpinBox( value=chunkSize*1300, # if change, change also in suffix 
                                int=True,
                                bounds=[chunkSize*100, None],
                                suffix=' Values to record ({:.2f} seconds)'.format(chunkSize * 1300 / 666000),
                                step=chunkSize*100, decimals=12, siPrefix=True)
        self.record_box.addWidget(self.spin)
        self.record_name_textbox = QtGui.QLineEdit(self)
        self.record_name_textbox.setText('lungs')
        record_name = self.record_name_textbox.text()
        self.record_box.addWidget(self.record_name_textbox)
        self.record_values_button = QtGui.QPushButton('Record Values')
        self.record_box.addWidget(self.record_values_button)
        self.layout.addLayout(self.record_box)

        self.progress = QtGui.QProgressBar()
        self.layout.addWidget(self.progress)


        self.glayout = pg.GraphicsLayoutWidget()
        # self.view = self.glayout.addViewBox(lockAspect=False)
        self.view = self.glayout.addViewBox(lockAspect=True)
        self.img = pg.ImageItem(border='w')
        self.view.addItem(self.img)
        # self.view.setAspectLocked()
        # bipolar colormap
        pos = np.array([0., 1., 0.5, 0.25, 0.75])
        color = np.array([[0,255,255,255], [255,255,0,255], [0,0,0,255], [0, 0, 255, 255], [255, 0, 0, 255]], dtype=np.ubyte)
        cmap = pg.ColorMap(pos, color)
        lut = cmap.getLookupTable(0.0, 1.0, 256)
        # set colormap
        self.img.setLookupTable(lut)
        # self.img.setLevels([-140, -50])
        self.img.setLevels([-50, 20])
        self.layout.addWidget(self.glayout)

        self.setLayout(self.layout)
        self.setGeometry(10, 10, 600, 1000)
        self.show()

    def qt_connections(self):
        self.record_values_button.clicked.connect(self.record_values_button_clicked)
        self.spin.valueChanged.connect(self.spinbox_value_changed)
        self.fft_chunks_slider.valueChanged.connect(self.fft_slider_changed)
        self.plot_points_x_slider.valueChanged.connect(self.plot_points_x_slider_changed)
        self.plot_points_y_slider.valueChanged.connect(self.plot_points_y_slider_changed)
        self.overlap_slider.valueChanged.connect(self.overlap_slider_slider_changed)
        self.record_name_textbox.textChanged.connect(self.record_name_changed)
        self.data_collected.connect(self.updateplot)
        self.chunk_recorded.connect(self.update_record_progress_bar)
        self.make_plots_button.clicked.connect(self.make_plots)

    def mkp2():
        self.data_collected.disconnect()
        # record_file_name = QtGui.QFileDialog.getOpenFileName(self, 'OpenFile')[0]
        # record_file_name = QtGui.QFileDialog.getOpenFileName()[0]
        # fileName, _ = QtGui.QFileDialog.getOpenFileName(self,"QFileDialog.getOpenFileName()", "", "Wave Files (*.wav)")
        # exec(open("./abc.py").read())

        fileName = '/Users/tandav/Documents/Ultrasonic-Stethoscope/data-temp/lungs-0.wav'
        print(fileName)
        if fileName:
            fs, y = wavfile.read(fileName)
            n = len(y) # length of the signal
            record_time = n / fs
            fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(16, 9))

            if self.signal_checkbox.isChecked():
                t = np.linspace(0, record_time, n) # time vector
                ax1.plot(t, y, 'b')
                # ax[0].plot(t[::100], y[::100], 'b')
                ax3.set_title('Signal')
                ax1.set_xlabel('Time: {0}seconds'.format(record_time))
                ax1.set_ylabel('Amplitude')
                ax1.grid()

            plt.tight_layout()
            plt.savefig(fileName[-3:] + 'png')


        self.data_collected.connect(self.updateplot)

    def make_plots(self):
        t3 = threading.Thread(target=self.mkp2)
        t3.start()


    def fft_slider_changed(self):
        global NFFT, chunkSize
        # self.NFFT = self.fft_chunks_slider.value() * self.chunkSize
        # self.fft_slider_label.setText('FFT window: {}'.format(self.NFFT))
        NFFT = 2 ** self.fft_chunks_slider.value()
        self.fft_slider_label.setText('FFT window: {}'.format(NFFT))
        self.t = np.linspace(0, (NFFT - 1) * 1e-6, NFFT)
        self.y = np.zeros(NFFT)
        self.f = np.zeros(NFFT // 2)
        self.a = np.zeros(NFFT // 2)
        self.win = np.hanning(NFFT)
        # self.win = np.blackman(NFFT)
        self.avg_sum = 0
        self.avg_iters = 0
        self.overlap_slider.setRange(0, NFFT - 1) # max is ser_reader_thread.chunks
        overlap = NFFT // 2
        self.overlap_slider.setValue(overlap)

    def plot_points_x_slider_changed(self):
        self.plot_points_x = self.plot_points_x_slider.value()
        self.plot_points_x_slider_label.setText('plot_points_x: {}'.format(self.plot_points_x))
        self.img_array = np.zeros((self.plot_points_x, self.plot_points_y)) # rename to (plot_width, plot_height)

    def plot_points_y_slider_changed(self):
        self.plot_points_y = self.plot_points_y_slider.value()
        self.plot_points_y_slider_label.setText('plot_points_y: {}'.format(self.plot_points_y))
        self.img_array = np.zeros((self.plot_points_x, self.plot_points_y)) # rename to (plot_width, plot_height)

    def overlap_slider_slider_changed(self):
        global overlap
        overlap = self.overlap_slider.value()
        self.overlap_slider_label.setText('FFT window overlap: {}'.format(overlap))

    def record_name_changed(self):
        global record_name
        record_name = self.record_name_textbox.text()

    @QtCore.pyqtSlot()
    def updateplot(self):
        t0 = time.time()
        global ser_reader_thread, recording, values_to_record, record_start_time, NFFT, big_dt

        self.t, self.y, self.rate = ser_reader_thread.get(num=NFFT) # MAX num=chunks*chunkSize (in SerialReader class)

        self.a = (fft(self.y * self.win) / NFFT)[:NFFT//2] # fft + chose only real part

        # в 2 строчки быстрее чем в одну! я замерял!
        self.a = np.abs(self.a) # magnitude
        self.a = 20 * np.log10(self.a) # часто ошибка - сделать try, else

        # spectrogram
        self.img_array = np.roll(self.img_array, -1, 0)
        if len(self.a) > self.plot_points_y:
            self.img_array[-1] = self.a[:self.plot_points_y]
        else:
            self.plot_points_y = len(a)
            self.img_array = np.zeros((self.plot_points_x, self.plot_points_y)) # rename to (plot_width, plot_height)
            self.img_array[-1] = self.a
        self.img.setImage(self.img_array, autoLevels=True)
        
        pp = 4096*2 # number of points to plot
        t_for_plot = self.t.reshape(pp, NFFT // pp).mean(axis=1)
        y_for_plot = self.y.reshape(pp, NFFT // pp).mean(axis=1)

        self.signal_curve.setData(t_for_plot, y_for_plot)
        self.signal_widget.getPlotItem().setTitle('Sample Rate: %0.2f'%self.rate)
        if self.rate > 0:
            self.f = np.fft.rfftfreq(NFFT - 1, d = 1. / self.rate)
            f_for_plot = self.f.reshape(pp, NFFT // pp // 2).mean(axis=1)
            a_for_plot = self.a.reshape(pp, NFFT // pp // 2).mean(axis=1)
            self.fft_curve.setData(f_for_plot, a_for_plot)



        t1 = time.time()
        self.avg_sum += t1 - t0
        self.avg_iters += 1
        # print('avg_dt=', self.avg_sum / self.avg_iters, 'iters=', self.avg_iters)
        if self.avg_iters % 10 == 0:
            # print('avg_dt=', self.avg_sum * 1000 / self.avg_iters, 'iters=', self.avg_iters)
            # print('big_dt =', (time.time() - big_dt) * 1000, '\tupdateplot_dt =', (t1 - t0) * 1000)

            print('big_dt: {:.3f}ms | updateplot_dt: {:.3f}ms | avg_dt: {:.3f} | iters: {}'.format((time.time() - big_dt) * 1000,
                                                                      (t1 - t0) * 1000,
                                                                       self.avg_sum * 1000 / self.avg_iters,
                                                                       self.avg_iters))
            if abs((time.time() - big_dt) - (t1 - t0)) < 0.010:
                print('WARNING: too big overlap: {:.3f}ms'.format(abs((time.time() - big_dt) - (t1 - t0)) * 1000))
        big_dt = time.time()

        # print(t1 - t0)
        # print('>>>>>')

    @QtCore.pyqtSlot()
    def update_record_progress_bar(self):
        global ser_reader_thread, recording, values_to_record, record_start_time

        rate = ser_reader_thread.sps
        while recording:
            self.progress.setValue(100 / (values_to_record / rate) * (time.time() - record_start_time)) # map recorded/to_record => 0% - 100%
            QApplication.processEvents() 
            time.sleep(0.01)
        self.progress.setValue(0)

    def spinbox_value_changed(self):
        self.spin.setSuffix(' Values to record' + ' ({:.2f} seconds)'.format(self.spin.value() / ser_reader_thread.sps))

    def keyPressEvent(self, event):
        if type(event) == QtGui.QKeyEvent and event.key() == QtCore.Qt.Key_Space:
            #here accept the event and do something
            self.record_values_button_clicked()
            event.accept()
        else:
            event.ignore()

    def record_values_button_clicked(self):
        global recording, values_to_record, record_start_time, record_buffer
        values_to_record = self.spin.value()
        record_buffer = np.empty(values_to_record)
        recording = True

        record_start_time = time.time()
        self.chunk_recorded.emit()

        # self.update_record_progress_bar()

    def closeEvent(self, event):
        global ser_reader_thread
        ser_reader_thread.exit()



def write_to_file(arr, ext, gzip=False):
        global file_index, record_name
        sys.stdout.write('start write to file ' + str(len(arr)) + ' values...')
        sys.stdout.flush()


        data_dir = 'data-temp/'
        # fileprefix = 'fio-disease-'
        fileprefix = record_name + '-'

        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        filename = data_dir + fileprefix + str(file_index) + '.' + ext


        if ext == 'dat':
            with open(filename, 'w') as f:
                arr.tofile(f)
        elif ext == 'txt':
            np.savetxt(filename, arr)
        elif ext == 'wav':
            rate = int(arr[1])
            arr = arr[2:] # del record_time and rate
            # scaled = np.int16(arr / np.max(np.abs(arr)) * 32767)
            # write_wav(filename, rate, scaled)
            write_wav(filename, rate, arr)
        else:
            print('wrong file extension')

        file_index += 1

        filesize = os.stat(filename).st_size
        print(" done (", filesize, ' bytes)', sep='')
        print(filename)
        if gzip:
            sys.stdout.write('gzip data compression: ' + str(filesize / 1000000) + 'MB...')
            sys.stdout.flush()

            with open(filename, 'rb') as f_in, gzip.open(filename + '.gz', 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            gzfilesize = os.stat(filename + '.gz').st_size
            print(' done. File reduced to ', gzfilesize / 1000000, 'MB (%0.0f' % (gzfilesize/filesize*100), '% of uncompressed)', sep='')


def send_to_cuda():
        global record_buffer, record_time, rate, record_start_time, record_end_time
        
        # old 
        # record_buffer = record_buffer.astype(np.float32) * (3.3 / 2**12) # Convert array to float and rescale to voltage. Assume 3.3V / 12bits
        # new: add rescale to [-1, 1]
        record_buffer = record_buffer.astype(np.float32) * (3.3 / 2**12) * 2 / 3.3 - 1# Convert array to float and rescale to voltage. Assume 3.3V / 12bits
        

        n = len(record_buffer) # length of the signal

        record_time = np.float32(record_end_time - record_start_time)
        rate = np.float32(n / record_time)
        sys.stdout.write('record time: ' + str(record_time) + 's\t' + 'rate: ' + str(rate) + 'sps   ' + str(len(record_buffer)) + ' values\n')

        # calc_fft_localy(record_buffer, n, record_time, rate)
        record_buffer = np.insert(record_buffer, 0, [record_time, rate]) # first two entries in file are record_time and rate
        # write_to_file(record_buffer, compression=False)
        write_to_file(record_buffer, 'wav', gzip=False)

        # print('start sending data to CUDA server...')
        # s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # s.connect(('192.168.119.170', 5005))  # (TCP_IP, TCP_PORT)
        # blocksize = 8192 # or some other size packet you want to transmit. Powers of 2 are good.
        # with open('signal.dat.gz', 'rb') as f:
        #     packet = f.read(blocksize)
        #     i = 0
        #     while packet:
        #         s.send(packet)
        #         packet = f.read(blocksize)
        #         i += 1
        #         if i % 100 == 0:
        #             print('data send: %0.0f' % (f.tell() / gzfilesize * 100), '%')
        # print('data send: 100% - success')
        # s.close()

        print('session end\n')

def main():
    # globals
    global recording, values_to_record, file_index, gui, ser_reader_thread, chunkSize, big_dt
    recording        = False
    values_to_record = 0
    file_index       = 0
    plot_points_x    = 256
    chunkSize        = 1024
    chunks           = 2000
    big_dt           = 0

    # init gui
    app = QtGui.QApplication(sys.argv)
    gui = AppGUI(plot_points_x=plot_points_x) # create class instance

    # init and run serial arduino reader
    ser_reader_thread = SerialReader(data_collected_signal=gui.data_collected, 
                                     chunkSize=chunkSize,
                                     chunks=chunks)
    ser_reader_thread.start()

    # app exit
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
