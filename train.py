# -*- coding: utf-8 -*-
"""vldm_train.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1RZgHp3Fvoax74NTjAsMFivo4SwiDu5g9
"""

# !pip install diffusers datasets accelerate torch_xla opencv-python
# 
# !pip install diffusers einops opencv-python-headless accelerate
# !git clone https://github.com/srpkdyy/VideoLDM.git
# !sed -i 's/num_attention_heads/num_attention_heads/g' VideoLDM/*.py
# 
# !wget https://ak.picdn.net/shutterstock/videos/31221946/preview/stock-footage-fun-clown-d-animation.mp4

import os

from dataclasses import dataclass
@dataclass
class TrainingConfig:
    image_size = 128  # the generated image resolution
    train_batch_size = 16
    eval_batch_size = 16  # how many images to sample during evaluation
    num_epochs = 50
    gradient_accumulation_steps = 1
    learning_rate = 1e-4
    lr_warmup_steps = 500
    save_image_epochs = 10
    save_model_epochs = 30
    mixed_precision = "fp16"  # `no` for float32, `fp16` for automatic mixed precision
    output_dir = "ddpm-butterflies-128"  # the model name locally and on the HF Hub


    push_to_hub = False  # whether to upload the saved model to the HF Hub
    hub_private_repo = False
    overwrite_output_dir = True  # overwrite the old model when re-running the notebook
    seed = 0


config = TrainingConfig()

# Load the video
import cv2
from PIL import Image

video_path = '../stock-footage-fun-clown-d-animation.mp4'
cap = cv2.VideoCapture(video_path)

dataset = []
movie = []  # List to store the frames
while True:
    # Read a new frame
    success, frame = cap.read()
    if not success:
        break  # If there are no more frames, exit the loop

    # Convert frame from BGR to RGB (OpenCV uses BGR by default)
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    # Append the RGB frame to the list
    img = Image.fromarray(rgb_frame)
    movie.append(img)
dataset.append(movie)


# Release the video capture object
cap.release()

import matplotlib.pyplot as plt

fig, axs = plt.subplots(1, 4, figsize=(16, 4))


for i, image in enumerate(dataset[0][:4]):
    axs[i].imshow(image)
    axs[i].set_axis_off()

fig.show()

from torchvision import transforms

preprocess = transforms.Compose(
    [
        transforms.Resize((config.image_size, config.image_size)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize([0.5], [0.5]),
    ]
)

import torch
def transform(examples):
    images = [preprocess(image) for image in examples]
    return images


dataset[0] = transform(dataset[0])

import torch

print(len(dataset))

train_dataloader = torch.utils.data.DataLoader(dataset, batch_size=config.train_batch_size, shuffle=True)

from videoldm import VideoLDM

model = VideoLDM.from_pretrained(
    'runwayml/stable-diffusion-v1-5',
    subfolder='unet',
    low_cpu_mem_usage=False
)

from transformers import AutoTokenizer, CLIPTextModel


tokenizer = AutoTokenizer.from_pretrained(
    'runwayml/stable-diffusion-v1-5',
    subfolder="tokenizer",
    revision=None,
)

text_encoder = CLIPTextModel.from_pretrained(
    'runwayml/stable-diffusion-v1-5',
    subfolder="text_encoder",
    revision=None,
)

import random

def choose_random_frames(images, frames):
    # Ensure there are enough images to choose from
    if frames > len(images):
        raise ValueError("The number of frames requested exceeds the number of available images.")
    # Choose a random start index
    i = random.randint(0, len(images) - frames)
    # Select the sequence of frames
    selected_images = images[i:i + frames]
    # Stack the selected frames along a new dimension
    stacked_images = torch.stack(selected_images, dim=0)
    return stacked_images



inputs = tokenizer(["fun clown juggling gas jugs"], padding=True, return_tensors="pt")

outputs = text_encoder(**inputs)
last_hidden_state = outputs.last_hidden_state

sample_image = choose_random_frames(dataset[0], 4)
single_it = model(sample_image, timestep=4, encoder_hidden_states=last_hidden_state)

### HERE

import torch
from PIL import Image
from diffusers import DDPMScheduler
noise_scheduler = DDPMScheduler(num_train_timesteps=1000)
noise = torch.randn(sample_image.shape)
timesteps = torch.LongTensor([50])
noisy_image = noise_scheduler.add_noise(sample_image, noise, timesteps)
Image.fromarray(((noisy_image.permute(0, 2, 3, 1) + 1.0) * 127.5).type(torch.uint8).numpy()[0])

import torch.nn.functional as F

noise_pred = model(noisy_image, timesteps).sample

loss = F.mse_loss(noise_pred, noise)

from diffusers.optimization import get_cosine_schedule_with_warmup

optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate)

lr_scheduler = get_cosine_schedule_with_warmup(
    optimizer=optimizer,
    num_warmup_steps=config.lr_warmup_steps,
    num_training_steps=(len(train_dataloader) * config.num_epochs),
)

from diffusers import DDPMPipeline

from diffusers.utils import make_image_grid

import os

def evaluate(config, epoch, pipeline):

    # Sample some images from random noise (this is the backward diffusion process).

    # The default pipeline output type is `List[PIL.Image]`

    images = pipeline(

        batch_size=config.eval_batch_size,

        generator=torch.manual_seed(config.seed),

    ).images

    # Make a grid out of the images

    image_grid = make_image_grid(images, rows=4, cols=4)

    # Save the images

    test_dir = os.path.join(config.output_dir, "samples")

    os.makedirs(test_dir, exist_ok=True)

    image_grid.save(f"{test_dir}/{epoch:04d}.png")

from accelerate import Accelerator
from huggingface_hub import create_repo, upload_folder
from tqdm.auto import tqdm
from pathlib import Path
import os

def train_loop(config, model, noise_scheduler, optimizer, train_dataloader, lr_scheduler):
    # Initialize accelerator and tensorboard logging
    accelerator = Accelerator(
        mixed_precision=config.mixed_precision,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        log_with="tensorboard",
        project_dir=os.path.join(config.output_dir, "logs"),
    )
    if accelerator.is_main_process:
        if config.output_dir is not None:
            os.makedirs(config.output_dir, exist_ok=True)
        accelerator.init_trackers("train_example")

    # Prepare everything
    # There is no specific order to remember, you just need to unpack the
    # objects in the same order you gave them to the prepare method.
    model, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, lr_scheduler
    )

    global_step = 0

    # Now you train the model
    for epoch in range(config.num_epochs):
        progress_bar = tqdm(total=len(train_dataloader), disable=not accelerator.is_local_main_process)
        progress_bar.set_description(f"Epoch {epoch}")

        for step, batch in enumerate(train_dataloader):
            clean_images = batch["images"]
            # Sample noise to add to the images
            noise = torch.randn(clean_images.shape, device=clean_images.device)
            bs = clean_images.shape[0]

            # Sample a random timestep for each image
            timesteps = torch.randint(
                0, noise_scheduler.config.num_train_timesteps, (bs,), device=clean_images.device,
                dtype=torch.int64
            )

            # Add noise to the clean images according to the noise magnitude at each timestep
            # (this is the forward diffusion process)
            noisy_images = noise_scheduler.add_noise(clean_images, noise, timesteps)

            with accelerator.accumulate(model):
                # Predict the noise residual
                noise_pred = model(noisy_images, timesteps, return_dict=False)[0]
                loss = F.mse_loss(noise_pred, noise)
                accelerator.backward(loss)

                accelerator.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            progress_bar.update(1)
            logs = {"loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0], "step": global_step}
            progress_bar.set_postfix(**logs)
            accelerator.log(logs, step=global_step)
            global_step += 1

        # After each epoch you optionally sample some demo images with evaluate() and save the model
        if accelerator.is_main_process:
            pipeline = DDPMPipeline(unet=accelerator.unwrap_model(model), scheduler=noise_scheduler)

            if (epoch + 1) % config.save_image_epochs == 0 or epoch == config.num_epochs - 1:
                evaluate(config, epoch, pipeline)

            if (epoch + 1) % config.save_model_epochs == 0 or epoch == config.num_epochs - 1:
                pipeline.save_pretrained(config.output_dir)

from accelerate import notebook_launcher

args = (config, model, noise_scheduler, optimizer, train_dataloader, lr_scheduler)

notebook_launcher(train_loop, args, num_processes=1)

import glob

sample_images = sorted(glob.glob(f"{config.output_dir}/samples/*.png"))
Image.open(sample_images[-1])
