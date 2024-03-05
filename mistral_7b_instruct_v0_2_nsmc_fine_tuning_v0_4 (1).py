# -*- coding: utf-8 -*-
"""mistral-7B-instruct-v0.2-nsmc-fine-tuning-v0.4.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1BsqWodH7DAFUGYHQcHOpLXkAh3Htdp2m

- food-order-understanding-small-3200.json (학습)
- food-order-understanding-small-800.json (검증)
- 로깅을 위한 wandb

mistral model
https://huggingface.co/mistralai/Mistral-7B-Instruct-v0.2

# 1. 세팅
"""

pip install transformers peft accelerate optimum bitsandbytes trl wandb

import os
from dataclasses import dataclass, field
from typing import Optional
import re

import torch
import tyro
from accelerate import Accelerator
from datasets import load_dataset, Dataset
from peft import AutoPeftModelForCausalLM, LoraConfig
from tqdm import tqdm
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
)

from trl import SFTTrainer

from trl.trainer import ConstantLengthDataset

from huggingface_hub import notebook_login

notebook_login()

"""드라이브 마운트 후 파일 업로드
- food-order-understanding-small-3200.json
- food-order-understanding-small-800.json
"""

from google.colab import drive
drive.mount('/gdrive')

"""# 2. 매개 변수 설정
- 어텐션 메커니즘 참고: https://github.com/mistralai/mistral-src/blob/main/mistral/model.py
"""

@dataclass
class ScriptArguments:
    cache_dir: Optional[str] = field(
        default=None, metadata={"help": "the cache dir"}
    )
    model_name: Optional[str] = field(
        # 수정 필요? mistralai/Mistral-7B-Instruct-v0.2
        default="mistralai/Mistral-7B-Instruct-v0.2", metadata={"help": "the model name"}
    )

    dataset_name: Optional[str] = field(
        default=None,
        metadata={"help": "the dataset name"},
    )
    seq_length: Optional[int] = field(
        default=1024, metadata={"help": "the sequence length"}
    )
    num_workers: Optional[int] = field(
        default=8, metadata={"help": "the number of workers"}
    )
    training_args: TrainingArguments = field(
        default_factory=lambda: TrainingArguments(
            output_dir="./results",
            # max_steps=500,
            logging_steps=20,
            # save_steps=10,
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=2,
            gradient_checkpointing=False,
            group_by_length=False,
            learning_rate=1e-4,
            lr_scheduler_type="cosine",
            # warmup_steps=100,
            warmup_ratio=0.03,
            max_grad_norm=0.3,
            weight_decay=0.05,
            save_total_limit=20,
            save_strategy="epoch",
            num_train_epochs=1,
            optim="paged_adamw_32bit",
            fp16=True,
            remove_unused_columns=False,
            report_to="wandb",
        )
    )

    packing: Optional[bool] = field(
        default=True, metadata={"help": "whether to use packing for SFTTrainer"}
    )

    peft_config: LoraConfig = field(
        default_factory=lambda: LoraConfig(
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "down_proj", "up_proj", "gate_proj"],
            bias="none",
            task_type="CAUSAL_LM",
        )
    )

    merge_with_final_checkpoint: Optional[bool] = field(
        default=False, metadata={"help": "Do only merge with final checkpoint"}
    )

"""# 3. 유틸리티"""

def chars_token_ratio(dataset, tokenizer, nb_examples=400):
    """
    Estimate the average number of characters per token in the dataset.
    """
    total_characters, total_tokens = 0, 0
    for _, example in tqdm(zip(range(nb_examples), iter(dataset)), total=nb_examples):
        text = prepare_sample_text(example)
        total_characters += len(text)
        if tokenizer.is_fast:
            total_tokens += len(tokenizer(text).tokens())
        else:
            total_tokens += len(tokenizer.tokenize(text))

    return total_characters / total_tokens


def print_trainable_parameters(model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    print(
        f"trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}"
    )

"""# 4. 데이터 로딩

수정

---


- Hugging Face의 mistral instruct format 예시에서는 사용자와 어시스턴트 간의 상호작용을 나타내는 채팅형 포맷을 보여주고 있음. 이는 다양한 대화형 태스크에서 매우 유용할 수 있으나, nsmc 데이터셋의 경우는 주어진 리뷰 텍스트에 대한 긍정/부정 분류가 주된 작업이므로, 어시스턴트의 대답을 생성하는 대신 리뷰 텍스트를 분석하여 긍정 또는 부정을 판단하는 것이 목표임.

- `<s>` 스페셜 토큰 관련
  - mistral 모델의 경우, 입력 포맷을 특정 지시문 형태로 구성할 때 명시적으로 `<s>` 토큰을 사용하지 않고, 대신 [INST]와 [/INST] 토큰으로 지시문을 구분하는 방식을 채택 가능.
  - 모델이 내부적으로 텍스트의 시작과 끝을 인식하는 방식에 따라 다르므로, 명시적인 `<s>` 사용은 모델의 입력 처리 방식과 지시문 형식에 따라 결정

- 긍정/부정 분류 명령어 관련
  - mistral 모델을 사용할 때, 특히 instruction based 모델을 사용하는 경우, 모델에 직접적으로 "이 리뷰가 긍정인지 부정인지 분류하라"는 명령어를 입력 텍스트에 포함시키지 않고, 대신 리뷰 텍스트를 모델에 제공하며 모델의 학습된 패턴을 이용해 긍정 또는 부정을 판단하도록 할 수 있음
  -  mistral 모델을 사용하여 nsmc 데이터셋의 리뷰를 긍정 또는 부정으로 분류할 때, 모델에게 직접적인 지시문을 제공하기보다는 리뷰 텍스트를 입력으로 제공하고 모델이 해당 텍스트를 분석하여 긍정 또는 부정으로 분류하도록 함. 이 접근 방식은 모델이 지시문을 해석하고 적절한 출력을 생성할 수 있도록 하는 Hugging Face의 instruction fine-tuning 기능을 활용

- System 명령어 제거 이유
  - System 부분은 사용자와 어시스턴트 간의 상호작용을 나타내는 채팅형 포맷에서 주로 사용되며, 사용자에게 정보를 제공하거나 특정 작업을 지시할 때 유용
  -  NSMC 데이터셋을 사용하여 리뷰의 긍정 또는 부정을 판단하는 작업에 있어서, 리뷰 텍스트 자체와 그에 대한 모델의 분석이 주된 초점
  - System 부분을 통해 추가적인 지시를 제공하기보다는, 리뷰 텍스트(User)만을 사용하여 모델에게 분석 작업을 명확하게 지시하는 것이 목적에 더 부합
"""

def prepare_sample_text(example):
    """Prepare the text from a sample of the dataset using mistral instruct format."""

    # Mistral Instruct 포맷에 맞게 수정된 프롬프트 템플릿
    # User의 리뷰를 기반으로 긍정 또는 부정을 판단하는 지시문을 생성
    prompt_template = """[INST] 너는 사용자가 작성한 리뷰의 긍정 또는 부정을 판단해야 한다. 리뷰: "{User}" [/INST]\n반응: "{Assistant}" """ # 학습
    # prompt_template = """[INST] 너는 사용자가 작성한 리뷰의 긍정 또는 부정을 판단해야 한다. 리뷰: "{User}" [/INST]\n반응: """ # 테스트


    text = prompt_template.format(User=example["document"],
                                  Assistant="긍정" if example["label"]==1 else "부정")

    return text

"""### nsmc 데이터셋을 로드하는 부분
- train_data는 2000개 로드
- test_dataA는 1000개 로드
- 각각 for문 설정
"""

from datasets import load_dataset

def create_datasets(tokenizer, args):
    # 'nsmc' 데이터셋 로드
    nsmc_dataset = load_dataset('nsmc')

    # 학습 데이터셋 선택
    if 'train' in nsmc_dataset:
        train_data = nsmc_dataset['train'].shuffle(seed=42).select([i for i in range(2000)])
    else:
        train_data = nsmc_dataset.shuffle(seed=42).select([i for i in range(2000)])

    # 문자-토큰 비율 계산
    chars_per_token = chars_token_ratio(train_data, tokenizer)
    print(f"The character to token ratio of the dataset is: {chars_per_token:.2f}")

    # 학습 데이터셋 생성
    train_dataset = ConstantLengthDataset(
        tokenizer,
        train_data,
        formatting_func=prepare_sample_text,
        infinite=True,
        seq_length=args.seq_length,
        chars_per_token=chars_per_token,
    )

    return train_dataset

"""# 5. 미세 튜닝용 모델 로딩"""

script_args = ScriptArguments(
    num_workers=2,
    seq_length=256,
    dataset_name='/gdrive/MyDrive/nlp/food-order-understanding-small-3200.json',
    model_name='mistralai/Mistral-7B-instruct-v0.2',
    )

"""세부적인 과정을 보고자 step을 50으로 세분화"""

script_args.training_args.logging_steps = 50
script_args.training_args.max_steps = 2000
script_args.training_args.output_dir = '/gdrive/MyDrive/undergraduateReasearcher/mistral-instruct-v0.2-results-nsmc-consecutive'
script_args.training_args.run_name = 'mistral-instruct-v0.2-nsmc-2k-steps'

print(script_args)

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
)

base_model = AutoModelForCausalLM.from_pretrained(
    script_args.training_args.output_dir, #script_args.model_name,
    quantization_config=bnb_config,
    device_map="auto",  # {"": Accelerator().local_process_index},
    trust_remote_code=True,
    use_auth_token=True,
    cache_dir=script_args.cache_dir,
    torch_dtype=torch.float16
)
base_model.config.use_cache = False

base_model

peft_config = script_args.peft_config

peft_config

tokenizer = AutoTokenizer.from_pretrained(
    script_args.model_name,
    trust_remote_code=True,
    cache_dir=script_args.cache_dir,
)

if getattr(tokenizer, "pad_token", None) is None:
    tokenizer.pad_token = tokenizer.eos_token
tokenizer.padding_side = "right"  # Fix weird overflow issue with fp16 training

base_model.config.pad_token_id = tokenizer.pad_token_id

"""`<s>`, `</s>` 스페셜 토큰 첨부 여부 확인
- tokenizer를 통해 예시 리뷰의 정보를 출력하여 모델이 기본적으로 bos_token과 eos_token 정보를 생성하는지 확인 필요
- 이에 따른 프롬프트 명령어 토큰 조정 필요(mistral을 비롯한 모든 LLM Model에 적용되는 사항)
"""

tokenizer.bos_token_id, tokenizer.eos_token_id

tokenizer("굳ㅋ", add_special_tokens=True, truncation=False)

training_args = script_args.training_args

train_dataset = create_datasets(tokenizer, script_args)

print(len(train_dataset))

"""- SFTTrainer
  - 모델 학습 과정을 커스터마이즈하고, 특히 PEFT(Progressive Effort Fine-Tuning) 방식이나 LoRA(Low-Rank Adaptation) 같은 고급 미세조정 기법을 적용할 때 유용
"""

trainer = SFTTrainer(
    model=base_model,
    train_dataset=train_dataset,
    eval_dataset=None,
    peft_config=peft_config,
    packing=script_args.packing,
    max_seq_length=script_args.seq_length,
    tokenizer=tokenizer,
    args=training_args,
)

base_model

print_trainable_parameters(base_model)

"""시퀀스 길이 512의 경우
- 14.4 G / 15.0 G 사용
- 메모리 오버플로우 발생시 512보다 줄일 것
"""

trainer.train()

script_args.training_args.output_dir

trainer.save_model(script_args.training_args.output_dir)

"""# 6. 추론 테스트

앞서 실행한 prepare_sample_text의 프롬프트와 동일화
"""

from transformers import pipeline, TextStreamer

"""수정 후"""

# 최대한 원본을 벗어나지 않게끔 수정하는 방안
instruction_prompt_template = "[INST] 너는 사용자가 작성한 리뷰의 긍정 또는 부정을 판단해야 한다. 리뷰: 굳ㅋ [/INST]\n반응: 긍정"

prompt_template = """[INST] 너는 사용자가 작성한 리뷰의 긍정 또는 부정을 판단해야 한다. 리뷰: "{User}" [/INST]\n반응: """

default_system_msg = (
    "너는 사용자가 작성한 리뷰의 긍정 또는 부정을 판단해야 한다."
)

"""허깅페이스에 업로드 되어있는 nsmc 데이터셋에서 상위 11개 직접 입력
https://huggingface.co/datasets/nsmc
"""

# 수정 후
evaluation_queries = load_dataset('nsmc', split='test[:1000]')

def wrapper_generate(model, input_prompt):
    data = tokenizer(input_prompt, return_tensors="pt")
    streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    input_ids = data.input_ids[..., :-1]
    # print(input_prompt, input_ids)
    with torch.no_grad():
        pred = model.generate(
            input_ids=input_ids.cuda(),
            streamer=streamer,
            use_cache=True,
            max_new_tokens=float('inf'),
            temperature=0.5
        )
    decoded_text = tokenizer.batch_decode(pred, skip_special_tokens=True)
    print(decoded_text)
    return (decoded_text[0][len(input_prompt):])

eval_dic = {
    i: (query, wrapper_generate(model=base_model, input_prompt=prompt_template.format(User=query['document'])))
    for i, query in enumerate(evaluation_queries.select(range(10)))
}

eval_dic

eval_dic = {
    i: (query, wrapper_generate(model=base_model, input_prompt=prompt_template.format(User=query['document'])))
    for i, query in enumerate(evaluation_queries)
}

print(eval_dic[0])

eval_dic

import pickle

# 특정 디렉토리에 eval_dic.pkl로 저장
save_path = '/gdrive/MyDrive/undergraduateReasearcher/mistral-7B-instruct-v0.2-nsmc-eval-dict/base-model-eval-dict-pickle-v0.1'  # 전체 경로 지정
with open(save_path, 'wb') as f:
    pickle.dump(eval_dic, f)

# 파일에서 eval_dic 불러오기(확인용)
with open(save_path, 'rb') as f:
    loaded_eval_dic = pickle.load(f)

# 불러온 딕셔너리 사용
print(loaded_eval_dic)