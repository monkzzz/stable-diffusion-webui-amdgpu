import json
import torch
import numpy as np
import shutil
import inspect
import onnxruntime as ort
from pathlib import Path
from diffusers import OnnxRuntimeModel, OnnxStableDiffusionPipeline, StableDiffusionPipeline
from diffusers.pipelines.stable_diffusion import StableDiffusionPipelineOutput
from diffusers.pipelines.onnx_utils import ORT_TO_NP_TYPE

from olive.model import ONNXModel
from olive.workflows import run as olive_run

from modules import shared, images, devices, sd_samplers
from modules.paths_internal import sd_configs_path, models_path
from modules.sd_olive_scripts import get_base_model_name
from modules.processing import Processed, get_fixed_seed

def __call__(
    self: OnnxStableDiffusionPipeline,
    p,
    prompt = None,
    height = 512,
    width = 512,
    num_inference_steps = 50,
    guidance_scale = 7.5,
    negative_prompt = None,
    num_images_per_prompt = 1,
    eta = 0.0,
    generator = None,
    latents = None,
    prompt_embeds = None,
    negative_prompt_embeds = None,
    output_type = "pil",
    return_dict: bool = True,
    callback = None,
    callback_steps: int = 1,
    scheduler = None,
):
    # check inputs. Raise error if not correct
    self.check_inputs(
        prompt, height, width, callback_steps, negative_prompt, prompt_embeds, negative_prompt_embeds
    )

    # define call parameters
    if prompt is not None and isinstance(prompt, str):
        batch_size = 1
    elif prompt is not None and isinstance(prompt, list):
        batch_size = len(prompt)
    else:
        batch_size = prompt_embeds.shape[0]

    if generator is None:
        generator = np.random

    # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
    # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
    # corresponds to doing no classifier free guidance.
    do_classifier_free_guidance = guidance_scale > 1.0

    prompt_embeds = self._encode_prompt(
        prompt,
        num_images_per_prompt,
        do_classifier_free_guidance,
        negative_prompt,
        prompt_embeds=prompt_embeds,
        negative_prompt_embeds=negative_prompt_embeds,
    )

    # get the initial random noise unless the user supplied it
    latents_dtype = prompt_embeds.dtype
    latents_shape = (batch_size * num_images_per_prompt, 4, height // 8, width // 8)
    if latents is None:
        latents = generator.randn(*latents_shape).astype(latents_dtype)
    elif latents.shape != latents_shape:
        raise ValueError(f"Unexpected latents shape, got {latents.shape}, expected {latents_shape}")

    # set timesteps
    self.scheduler.set_timesteps(num_inference_steps)

    latents = latents * np.float64(self.scheduler.init_noise_sigma)

    # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
    # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
    # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
    # and should be between [0, 1]
    accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
    extra_step_kwargs = {}
    if accepts_eta:
        extra_step_kwargs["eta"] = eta

    timestep_dtype = next(
        (input.type for input in self.unet.model.get_inputs() if input.name == "timestep"), "tensor(float)"
    )
    timestep_dtype = ORT_TO_NP_TYPE[timestep_dtype]

    for i, t in enumerate(self.progress_bar(self.scheduler.timesteps)):
        if shared.state.job_count == -1:
            shared.state.job_count = p.n_iter

        if shared.state.skipped:
            shared.state.skipped = False

        if shared.state.interrupted:
            break

        p.prompts = p.all_prompts[i * p.batch_size:(i + 1) * p.batch_size]
        p.negative_prompts = p.all_negative_prompts[i * p.batch_size:(i + 1) * p.batch_size]
        p.seeds = p.all_seeds[i * p.batch_size:(i + 1) * p.batch_size]
        p.subseeds = p.all_subseeds[i * p.batch_size:(i + 1) * p.batch_size]
        
        if p.scripts is not None:
            p.scripts.before_process_batch(p, batch_number=i, prompts=p.prompts, seeds=p.seeds, subseeds=p.subseeds)

        if len(p.prompts) == 0:
            break

        if p.scripts is not None:
            p.scripts.process_batch(p, batch_number=i, prompts=p.prompts, seeds=p.seeds, subseeds=p.subseeds)

        if p.n_iter > 1:
            shared.state.job = f"Batch {i+1} out of {p.n_iter}"

        # expand the latents if we are doing classifier free guidance
        latent_model_input = np.concatenate([latents] * 2) if do_classifier_free_guidance else latents
        latent_model_input = self.scheduler.scale_model_input(torch.from_numpy(latent_model_input), t)
        latent_model_input = latent_model_input.cpu().numpy()

        # predict the noise residual
        timestep = np.array([t], dtype=timestep_dtype)
        noise_pred = self.unet(sample=latent_model_input, timestep=timestep, encoder_hidden_states=prompt_embeds)
        noise_pred = noise_pred[0]

        # perform guidance
        if do_classifier_free_guidance:
            noise_pred_uncond, noise_pred_text = np.split(noise_pred, 2)
            noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

        # compute the previous noisy sample x_t -> x_t-1
        scheduler_output = self.scheduler.step(
            torch.from_numpy(noise_pred), t, torch.from_numpy(latents), **extra_step_kwargs
        )
        latents = scheduler_output.prev_sample.numpy()

        # call the callback, if provided
        if callback is not None and i % callback_steps == 0:
            callback(i, t, latents)

    latents = 1 / 0.18215 * latents
    # image = self.vae_decoder(latent_sample=latents)[0]
    # it seems likes there is a strange result for using half-precision vae decoder if batchsize>1
    image = np.concatenate(
        [self.vae_decoder(latent_sample=latents[i : i + 1])[0] for i in range(latents.shape[0])]
    )

    image = np.clip(image / 2 + 0.5, 0, 1)
    image = image.transpose((0, 2, 3, 1))

    if self.safety_checker is not None:
        safety_checker_input = self.feature_extractor(
            self.numpy_to_pil(image), return_tensors="np"
        ).pixel_values.astype(image.dtype)

        images, has_nsfw_concept = [], []
        for i in range(image.shape[0]):
            image_i, has_nsfw_concept_i = self.safety_checker(
                clip_input=safety_checker_input[i : i + 1], images=image[i : i + 1]
            )
            images.append(image_i)
            has_nsfw_concept.append(has_nsfw_concept_i[0])
        image = np.concatenate(images)
    else:
        has_nsfw_concept = None

    if output_type == "pil":
        image = self.numpy_to_pil(image)

    if not return_dict:
        return (image, has_nsfw_concept)

    return StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept)
OnnxStableDiffusionPipeline.__call__ = __call__

def convert_to_onnx(model: str, output_dir: str):
    pipeline = StableDiffusionPipeline.from_ckpt(Path(models_path) / "Stable-diffusion" / model, torch_dtype=torch.float32)
    pipeline.save_pretrained(Path(models_path) / "ONNX" / output_dir)

def optimize(model_id: str, unoptimized_dir: str, optimized_dir: str, olive_safety_checker, olive_text_encoder, olive_unet, olive_vae_decoder, olive_vae_encoder, use_fp16):
    unoptimized_dir = Path(models_path) / "ONNX" / unoptimized_dir
    optimized_dir = Path(models_path) / "ONNX-Olive" / optimized_dir
    
    shutil.rmtree("footprints", ignore_errors=True)
    shutil.rmtree(unoptimized_dir, ignore_errors=True)
    shutil.rmtree(optimized_dir, ignore_errors=True)

    base_model_id = get_base_model_name(model_id)

    pipeline = StableDiffusionPipeline.from_pretrained(base_model_id, torch_dtype=torch.float32)

    submodels = []
    model_info = {}

    if olive_safety_checker:
        submodels += ["safety_checker"]
    if olive_text_encoder:
        submodels += ["text_encoder"]
    if olive_unet:
        submodels += ["unet"]
    if olive_vae_decoder:
        submodels += ["vae_decoder"]
    if olive_vae_encoder:
        submodels += ["vae_encoder"]

    for submodel_name in submodels:
        print(f"\nOptimizing {submodel_name}")

        olive_config = None
        with open(Path(sd_configs_path) / "olive_optimize" / f"config_{submodel_name}.json", "r") as fin:
            olive_config = json.load(fin)

        olive_config["passes"]["optimize"]["config"]["float16"] = use_fp16
        if submodel_name in ("unet", "text_encoder"):
            olive_config["input_model"]["config"]["model_path"] = model_id
        else:
            olive_config["input_model"]["config"]["model_path"] = base_model_id

        olive_run(olive_config)

        footprints_file_path = (
            Path("footprints") / f"{submodel_name}_gpu-dml_footprints.json"
        )
        with footprints_file_path.open("r") as footprint_file:
            footprints = json.load(footprint_file)

            conversion_footprint = None
            optimizer_footprint = None
            for _, footprint in footprints.items():
                if footprint["from_pass"] == "OnnxConversion":
                    conversion_footprint = footprint
                elif footprint["from_pass"] == "OrtTransformersOptimization":
                    optimizer_footprint = footprint

            assert conversion_footprint and optimizer_footprint

            unoptimized_olive_model = ONNXModel(**conversion_footprint["model_config"]["config"])
            optimized_olive_model = ONNXModel(**optimizer_footprint["model_config"]["config"])

            model_info[submodel_name] = {
                "unoptimized": {
                    "path": Path(unoptimized_olive_model.model_path),
                },
                "optimized": {
                    "path": Path(optimized_olive_model.model_path),
                },
            }

            print(f"Optimized {submodel_name}")

    print("\nCreating ONNX pipeline...")
    onnx_pipeline = OnnxStableDiffusionPipeline(
        vae_encoder=OnnxRuntimeModel.from_pretrained(model_info["vae_encoder"]["unoptimized"]["path"].parent),
        vae_decoder=OnnxRuntimeModel.from_pretrained(model_info["vae_decoder"]["unoptimized"]["path"].parent),
        text_encoder=OnnxRuntimeModel.from_pretrained(model_info["text_encoder"]["unoptimized"]["path"].parent),
        tokenizer=pipeline.tokenizer,
        unet=OnnxRuntimeModel.from_pretrained(model_info["unet"]["unoptimized"]["path"].parent),
        scheduler=pipeline.scheduler,
        safety_checker=None if pipeline.feature_extractor is None else OnnxRuntimeModel.from_pretrained(model_info["safety_checker"]["unoptimized"]["path"].parent),
        feature_extractor=pipeline.feature_extractor,
        requires_safety_checker=True,
    )

    print("Saving unoptimized models...")
    onnx_pipeline.save_pretrained(unoptimized_dir)

    print("Copying optimized models...")
    shutil.copytree(unoptimized_dir, optimized_dir, ignore=shutil.ignore_patterns("weights.pb"))
    for submodel_name in submodels:
        try:
            src_path = model_info[submodel_name]["optimized"]["path"]
            dst_path = optimized_dir / submodel_name / "model.onnx"
            shutil.copyfile(src_path, dst_path)
        except:
            pass

    shared.refresh_checkpoints()
    print(f"Optimization complete.")

class OliveOptimizedModel:
    def __init__(self, path: str):
        self.path = path
        self.sd_model_hash = None

class OliveOptimizedProcessingTxt2Img:
    def __init__(self, sd_model=None, outpath_samples=None, outpath_grids=None, prompt: str = "", styles = None, seed: int = -1, subseed: int = -1, subseed_strength: float = 0, seed_resize_from_h: int = -1, seed_resize_from_w: int = -1, seed_enable_extras: bool = True, sampler_name: str = None, batch_size: int = 1, n_iter: int = 1, steps: int = 50, cfg_scale: float = 7.0, width: int = 512, height: int = 512, restore_faces: bool = False, tiling: bool = False, do_not_save_samples: bool = False, do_not_save_grid: bool = False, extra_generation_params = None, overlay_images = None, negative_prompt: str = None, eta: float = None, do_not_reload_embeddings: bool = False, ddim_discretize: str = None, s_min_uncond: float = 0.0, s_churn: float = 0.0, s_tmax: float = None, s_tmin: float = 0.0, s_noise: float = 1.0, override_settings = None, override_settings_restore_afterwards: bool = True, sampler_index: int = None, script_args: list = None, enable_hr: bool = False, denoising_strength: float = 0.75, firstphase_width: int = 0, firstphase_height: int = 0, hr_scale: float = 2.0, hr_upscaler: str = None, hr_second_pass_steps: int = 0, hr_resize_x: int = 0, hr_resize_y: int = 0, hr_sampler_name: str = None, hr_prompt: str = '', hr_negative_prompt: str = ''):
        self.sd_model: Path = Path(models_path) / "ONNX-Olive" / sd_model.path
        self.outpath_samples: str = outpath_samples
        self.outpath_grids: str = outpath_grids
        self.prompt: str = prompt
        self.prompt_for_display: str = None
        self.negative_prompt: str = (negative_prompt or "")
        self.styles: list = styles or []
        self.seed: int = seed
        self.subseed: int = subseed
        self.subseed_strength: float = subseed_strength
        self.seed_resize_from_h: int = seed_resize_from_h
        self.seed_resize_from_w: int = seed_resize_from_w
        self.sampler_name: str = sampler_name
        self.batch_size: int = batch_size
        self.n_iter: int = n_iter
        self.steps: int = steps
        self.cfg_scale: float = cfg_scale
        self.width: int = width
        self.height: int = height
        self.restore_faces: bool = restore_faces
        self.tiling: bool = tiling
        self.do_not_save_samples: bool = do_not_save_samples
        self.do_not_save_grid: bool = do_not_save_grid
        self.extra_generation_params: dict = extra_generation_params or {}
        self.overlay_images = overlay_images
        self.eta = eta
        self.do_not_reload_embeddings = do_not_reload_embeddings
        self.paste_to = None
        self.color_corrections = None
        self.denoising_strength: float = denoising_strength
        self.sampler_noise_scheduler_override = None
        self.ddim_discretize = ddim_discretize or shared.opts.ddim_discretize
        self.s_min_uncond = s_min_uncond or shared.opts.s_min_uncond
        self.s_churn = s_churn or shared.opts.s_churn
        self.s_tmin = s_tmin or shared.opts.s_tmin
        self.s_tmax = s_tmax or float('inf')  # not representable as a standard ui option
        self.s_noise = s_noise or shared.opts.s_noise
        self.override_settings = {k: v for k, v in (override_settings or {}).items() if k not in shared.restricted_opts}
        self.override_settings_restore_afterwards = override_settings_restore_afterwards
        self.is_using_inpainting_conditioning = False
        self.disable_extra_networks = False
        self.token_merging_ratio = 0
        self.token_merging_ratio_hr = 0

        if not seed_enable_extras:
            self.subseed = -1
            self.subseed_strength = 0
            self.seed_resize_from_h = 0
            self.seed_resize_from_w = 0

        self.scripts = None
        self.script_args = script_args
        self.all_prompts = None
        self.all_negative_prompts = None
        self.all_seeds = None
        self.all_subseeds = None
        self.iteration = 0
        self.is_hr_pass = False
        self.sampler = None

        self.prompts = None
        self.negative_prompts = None
        self.seeds = None
        self.subseeds = None

        self.step_multiplier = 1
        self.cached_uc = [None, None]
        self.cached_c = [None, None]
        self.uc = None
        self.c = None

        if type(prompt) == list:
            self.all_prompts = self.prompt
        else:
            self.all_prompts = self.batch_size * self.n_iter * [self.prompt]

        if type(self.negative_prompt) == list:
            self.all_negative_prompts = self.negative_prompt
        else:
            self.all_negative_prompts = self.batch_size * self.n_iter * [self.negative_prompt]

        self.all_prompts = [shared.prompt_styles.apply_styles_to_prompt(x, self.styles) for x in self.all_prompts]
        self.all_negative_prompts = [shared.prompt_styles.apply_negative_styles_to_prompt(x, self.styles) for x in self.all_negative_prompts]

        self.extra_generation_params: dict = {}
        self.override_settings = {k: v for k, v in ({}).items() if k not in shared.restricted_opts}
        self.override_settings_restore_afterwards = False

        self.enable_hr = enable_hr
        self.denoising_strength = denoising_strength
        self.hr_scale = hr_scale
        self.hr_upscaler = hr_upscaler
        self.hr_second_pass_steps = hr_second_pass_steps
        self.hr_resize_x = hr_resize_x
        self.hr_resize_y = hr_resize_y
        self.hr_upscale_to_x = hr_resize_x
        self.hr_upscale_to_y = hr_resize_y
        self.hr_sampler_name = hr_sampler_name
        self.hr_prompt = hr_prompt
        self.hr_negative_prompt = hr_negative_prompt
        self.all_hr_prompts = None
        self.all_hr_negative_prompts = None
        if firstphase_width != 0 or firstphase_height != 0:
            self.hr_upscale_to_x = self.width
            self.hr_upscale_to_y = self.height
            self.width = firstphase_width
            self.height = firstphase_height

        self.truncate_x = 0
        self.truncate_y = 0
        self.applied_old_hires_behavior_to = None

        self.hr_prompts = None
        self.hr_negative_prompts = None
        self.hr_extra_network_data = None

        self.hr_c = None
        self.hr_uc = None

        self.sess_options = ort.SessionOptions()
        self.sess_options.enable_mem_pattern = False
        self.sess_options.add_free_dimension_override_by_name("unet_sample_batch", self.batch_size * 2)
        self.sess_options.add_free_dimension_override_by_name("unet_sample_channels", 4)
        self.sess_options.add_free_dimension_override_by_name("unet_sample_height", 64)
        self.sess_options.add_free_dimension_override_by_name("unet_sample_width", 64)
        self.sess_options.add_free_dimension_override_by_name("unet_time_batch", 1)
        self.sess_options.add_free_dimension_override_by_name("unet_hidden_batch", self.batch_size * 2)
        self.sess_options.add_free_dimension_override_by_name("unet_hidden_sequence", 77)
        self.pipeline = OnnxStableDiffusionPipeline.from_pretrained(
            self.sd_model, provider="DmlExecutionProvider", sess_options=self.sess_options
        )

    def process(self) -> Processed:
        if type(self.prompt) == list:
            assert(len(self.prompt) > 0)
        else:
            assert self.prompt is not None

        devices.torch_gc()

        seed = get_fixed_seed(self.seed)
        subseed = get_fixed_seed(self.subseed)

        if type(seed) == list:
            self.all_seeds = seed
        else:
            self.all_seeds = [int(seed) + (x if self.subseed_strength == 0 else 0) for x in range(len(self.all_prompts))]

        if type(subseed) == list:
            self.all_subseeds = subseed
        else:
            self.all_subseeds = [int(subseed) + x for x in range(len(self.all_prompts))]

        output_images = []

        for i in range(self.batch_size):
            result = self.pipeline(self,
                [self.prompt] * self.batch_size,
                negative_prompt=self.negative_prompt,
                num_inference_steps=self.steps,
                generator=torch.Generator(),
                height=self.height,
                width=self.width,
            )
            image = result.images[0]
            images.save_image(image, self.outpath_samples, "")
            output_images.append(image)

            devices.torch_gc()
            shared.state.nextjob()

        index_of_first_image = 0
        unwanted_grid_because_of_img_count = len(output_images) < 2 and shared.opts.grid_only_if_multiple
        if (shared.opts.return_grid or shared.opts.grid_save) and not unwanted_grid_because_of_img_count:
            grid = images.image_grid(output_images, self.batch_size)

            if shared.opts.return_grid:
                output_images.insert(0, grid)
                index_of_first_image = 1

            if shared.opts.grid_save:
                images.save_image(grid, self.outpath_grids, "grid", self.all_seeds[0], self.all_prompts[0], shared.opts.grid_format, short_filename=not shared.opts.grid_extended_filename, grid=True)

        devices.torch_gc()

        return Processed(
            self,
            images_list=output_images,
            seed=self.all_seeds[0],
            info="",
            comments="",
            subseed=self.all_subseeds[0],
            index_of_first_image=index_of_first_image,
            infotexts=[],
        )

    def close(self):
        return