/*
  Simple Waveform generator with Arduino Due

    connect two push buttons to the digital pins 2 and 3
    with a 10 kilohm pulldown resistor to choose the waveform
    to send to the DAC0 and DAC1 channels
    connect a 10 kilohm potentiometer to A0 to control the
    signal frequency

*/

#include "Waveforms.h"

#define oneHzSample 1000000/maxSamplesNum  // sample for the 1Hz signal expressed in microseconds 



const int button0 = 2, button1 = 3;
volatile int wave0 = 0, wave1 = 0;

int i = 0;
int sample;


void setup() {
  analogWriteResolution(12);  // set the analog output resolution to 12 bit (4096 levels)
  analogReadResolution(12);   // set the analog input resolution to 12 bit
  Serial.begin(9600);
}

void loop() {
  // Read the the potentiometer and map the value  between the maximum and the minimum sample available
  // 1 Hz is the minimum freq for the complete wave
  // 170 Hz is the maximum freq for the complete wave. Measured considering the loop and the analogRead() time
//  sample = map(analogRead(A0), 0, 4095, 0, oneHzSample);
//  sample = constrain(t_sample, 0, oneHzSample);

  analogWrite(DAC0, waveformsTable[wave0][i]);  // write the selected waveform on DAC0
  analogWrite(DAC1, waveformsTable[wave1][i]);  // write the selected waveform on DAC1
  Serial.println(waveformsTable[wave0][i]);

  i++;
  if (i == maxSamplesNum) // Reset the counter to repeat the wave
    i = 0;

//  delayMicroseconds(sample);  // Hold the sample value for the sample time
}
