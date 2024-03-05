This is the sentiment classification code for nsmc using the Mistral-7B-Instruct-v0.2 model.

I have uploaded files where the process up to the inference test is in the fine-tuning file, and the evaluation process is executed in the test file.

The accuracy evaluation results are as follows:

|   |Precision|Recall|f1-score|Support|
|---|------|---|---|
| 0 |0.85|0.95|0.87|492|
| 1 |0.90|0.84|0.87|508|
|accuracy|    |    |0.87|1000|
|macro avg|0.87|0.87|0.87|1000|
|weighted avg|0.87|0.87|0.87|0.87|1000|

I conducted up to 2000 training steps, but I believe that if there is sufficient memory available, increasing the steps further can enhance the accuracy.

|Precision|Recall|f1-score|
|:---|---:|:---:|
|0.85|0.95|0.87|
|0.90|0.84|0.87|
| | |0.87|
