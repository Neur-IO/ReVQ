# ------------------------------------------------------------------------------
# ReVQ: Quantize-then-Rectify: Efficient VQ-VAE Training
# Copyright (c) 2025 Borui Zhang. All Rights Reserved.
# Licensed under the MIT License [see LICENSE for details]
# ------------------------------------------------------------------------------
# Copyright (2024) Bytedance Ltd. and/or its affiliates
# ------------------------------------------------------------------------------

import warnings

from typing import Sequence, Optional, Mapping, Text
import numpy as np
from scipy import linalg
import torch
import torch.nn.functional as F

from torchmetrics.functional import structural_similarity_index_measure as ssim_fn
from torchmetrics.functional import peak_signal_noise_ratio as psnr_fn
from .inception import get_inception_model
import lpips
import pyiqa


def get_covariance(sigma: torch.Tensor, total: torch.Tensor, num_examples: int) -> torch.Tensor:
    """Computes covariance of the input tensor.

    Args:
        sigma: A torch.Tensor, sum of outer products of input features.
        total: A torch.Tensor, sum of all input features.
        num_examples: An integer, number of examples in the input tensor.
    Returns:
        A torch.Tensor, covariance of the input tensor.
    """
    if num_examples == 0:
        return torch.zeros_like(sigma)

    sub_matrix = torch.outer(total, total)
    sub_matrix = sub_matrix / num_examples

    return (sigma - sub_matrix) / (num_examples - 1)


class VQGANEvaluator:
    def __init__(
        self,
        device,
        enable_rfid: bool = True,
        enable_inception_score: bool = True,
        enable_codebook_usage_measure: bool = False,
        enable_codebook_entropy_measure: bool = False,
        enable_ssim_psnr: bool = True,
        enable_lpips: bool = True,
        num_codebook_entries: int = 1024,
    ):
        """Initializes VQGAN Evaluator.

        Args:
            device: The device to use for evaluation.
            enable_rfid: A boolean, whether enabling rFID score.
            enable_inception_score: A boolean, whether enabling Inception Score.
            enable_codebook_usage_measure: A boolean, whether enabling codebook usage measure.
            enable_codebook_entropy_measure: A boolean, whether enabling codebook entropy measure.
            num_codebook_entries: An integer, the number of codebook entries.
        """
        self._device = device

        self._enable_rfid = enable_rfid
        self._enable_inception_score = enable_inception_score
        self._enable_codebook_usage_measure = enable_codebook_usage_measure
        self._enable_codebook_entropy_measure = enable_codebook_entropy_measure
        self._num_codebook_entries = num_codebook_entries
        self._enable_ssim_psnr = enable_ssim_psnr
        self._enable_lpips = enable_lpips
        
        if self._enable_lpips:
            self._lpips_fn = lpips.LPIPS(net='alex').to(device)
            self._lpips_total = 0.0

        # Variables related to Inception score and rFID.
        self._inception_model = None
        self._is_num_features = 0
        self._rfid_num_features = 0
        if self._enable_inception_score or self._enable_rfid:
            self._rfid_num_features = 2048
            self._is_num_features = 1008
            self._inception_model = get_inception_model().to(self._device)
            self._inception_model.eval()
            # pass
        self._is_eps = 1e-16
        self._rfid_eps = 1e-6

        self.reset_metrics()

    def reset_metrics(self):
        """Resets all metrics."""
        self._num_examples = 0
        self._num_updates = 0

        self._is_prob_total = torch.zeros(
            self._is_num_features, dtype=torch.float64, device=self._device
        )
        self._is_total_kl_d = torch.zeros(
            self._is_num_features, dtype=torch.float64, device=self._device
        )
        self._rfid_real_sigma = torch.zeros(
            (self._rfid_num_features, self._rfid_num_features),
            dtype=torch.float64, device=self._device
        )
        self._rfid_real_total = torch.zeros(
            self._rfid_num_features, dtype=torch.float64, device=self._device
        )
        self._rfid_fake_sigma = torch.zeros(
            (self._rfid_num_features, self._rfid_num_features),
            dtype=torch.float64, device=self._device
        )
        self._rfid_fake_total = torch.zeros(
            self._rfid_num_features, dtype=torch.float64, device=self._device
        )

        self._set_of_codebook_indices = set()
        self._codebook_frequencies = torch.zeros((self._num_codebook_entries), dtype=torch.float64, device=self._device)

        if self._enable_ssim_psnr:
            self._ssim_computer = pyiqa.create_metric(
                metric_name="ssim", device=self._device
            )
            self._psnr_computer = pyiqa.create_metric(
                metric_name="psnr",
                test_y_channel=True,
                data_range=1.0,
                color_space="rgb",
                device=self._device,
            )

        self._ssim_total = 0.0
        self._psnr_total = 0.0
        self._lpips_total = 0.0

    def update(
        self,
        real_images: torch.Tensor,
        fake_images: torch.Tensor,
        codebook_indices: Optional[torch.Tensor] = None
    ):
        """Updates the metrics with the given images.

        Args:
            real_images: A torch.Tensor, the real images.
            fake_images: A torch.Tensor, the fake images.
            codebook_indices: A torch.Tensor, the indices of the codebooks for each image.

        Raises:
            ValueError: If the fake images is not in RGB (3 channel).
            ValueError: If the fake and real images have different shape.
        """

        batch_size = real_images.shape[0]
        dim = tuple(range(1, real_images.ndim))
        self._num_examples += batch_size
        self._num_updates += 1

        if self._enable_inception_score or self._enable_rfid:
            # Quantize to uint8 as a real image.
            fake_inception_images = (fake_images * 255).to(torch.uint8)
            features_fake = self._inception_model(fake_inception_images)
            inception_logits_fake = features_fake["logits_unbiased"]
            inception_probabilities_fake = F.softmax(inception_logits_fake, dim=-1)
        
        if self._enable_inception_score:
            probabiliies_sum = torch.sum(inception_probabilities_fake, 0, dtype=torch.float64)

            log_prob = torch.log(inception_probabilities_fake + self._is_eps)
            if log_prob.dtype != inception_probabilities_fake.dtype:
                log_prob = log_prob.to(inception_probabilities_fake)
            kl_sum = torch.sum(inception_probabilities_fake * log_prob, 0, dtype=torch.float64)

            self._is_prob_total += probabiliies_sum
            self._is_total_kl_d += kl_sum

        if self._enable_rfid:
            real_inception_images = (real_images * 255).to(torch.uint8)
            features_real = self._inception_model(real_inception_images)
            if (features_real['2048'].shape[0] != features_fake['2048'].shape[0] or
                features_real['2048'].shape[1] != features_fake['2048'].shape[1]):
                raise ValueError(f"Number of features should be equal for real and fake.")

            for f_real, f_fake in zip(features_real['2048'], features_fake['2048']):
                self._rfid_real_total += f_real
                self._rfid_fake_total += f_fake

                self._rfid_real_sigma += torch.outer(f_real, f_real)
                self._rfid_fake_sigma += torch.outer(f_fake, f_fake)

        if self._enable_codebook_usage_measure:
            self._set_of_codebook_indices |= set(torch.unique(codebook_indices, sorted=False).tolist())

        if self._enable_codebook_entropy_measure:
            entries, counts = torch.unique(codebook_indices, sorted=False, return_counts=True)
            self._codebook_frequencies.index_add_(0, entries.int(), counts.double())

        if self._enable_ssim_psnr:
            real_images_clamped = real_images.clamp(0, 1)
            fake_images_clamped = fake_images.clamp(0, 1)

            for real_img, fake_img in zip(real_images_clamped, fake_images_clamped):
                ssim_score = self._ssim_computer(fake_img.unsqueeze(0), real_img.unsqueeze(0)).item()
                psnr_score = self._psnr_computer(fake_img.unsqueeze(0), real_img.unsqueeze(0)).item()

                self._ssim_total += ssim_score
                self._psnr_total += psnr_score
        
        if self._enable_lpips:
            real_lpips = (real_images * 2 - 1).clamp(-1, 1)
            fake_lpips = (fake_images * 2 - 1).clamp(-1, 1)
            lpips_batch = self._lpips_fn(real_lpips, fake_lpips)
            self._lpips_total += lpips_batch.sum().item()

    def result(self) -> Mapping[Text, torch.Tensor]:
        """Returns the evaluation result."""
        eval_score = {}

        if self._num_examples < 1:
            raise ValueError("No examples to evaluate.")
        
        if self._enable_inception_score:
            mean_probs = self._is_prob_total / self._num_examples
            log_mean_probs = torch.log(mean_probs + self._is_eps)
            if log_mean_probs.dtype != self._is_prob_total.dtype:
                log_mean_probs = log_mean_probs.to(self._is_prob_total)
            excess_entropy = self._is_prob_total * log_mean_probs
            avg_kl_d = torch.sum(self._is_total_kl_d - excess_entropy) / self._num_examples

            inception_score = torch.exp(avg_kl_d).item()
            eval_score["InceptionScore"] = inception_score

        ## rfid
        if self._enable_rfid:
            mu_real = self._rfid_real_total / self._num_examples
            mu_fake = self._rfid_fake_total / self._num_examples
            sigma_real = get_covariance(self._rfid_real_sigma, self._rfid_real_total, self._num_examples)
            sigma_fake = get_covariance(self._rfid_fake_sigma, self._rfid_fake_total, self._num_examples)

            mu_real, mu_fake = mu_real.cpu(), mu_fake.cpu()
            sigma_real, sigma_fake = sigma_real.cpu(), sigma_fake.cpu()

            diff = mu_real - mu_fake

            # Convert to numpy
            sigma_real_np = sigma_real.numpy()
            sigma_fake_np = sigma_fake.numpy()

            # First attempt to compute sqrtm
            covmean, _ = linalg.sqrtm(sigma_real_np.dot(sigma_fake_np), disp=False)

            # Check if covmean is finite
            if not np.isfinite(covmean).all():
                warnings.warn(
                    f"FID calculation produces singular product; adding {self._rfid_eps} to diagonal of cov estimates"
                )
                offset = np.eye(sigma_real_np.shape[0]) * self._rfid_eps
                covmean = linalg.sqrtm((sigma_real_np + offset).dot(sigma_fake_np + offset))

            if np.iscomplexobj(covmean):
                if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
                    m = np.max(np.abs(covmean.imag))
                    raise ValueError("Imaginary component {}".format(m))
                covmean = covmean.real

            tr_covmean = np.trace(covmean)

            rfid = float(diff.dot(diff).item() + torch.trace(sigma_real) + torch.trace(sigma_fake)
                         - 2 * tr_covmean)

            if torch.isnan(torch.tensor(rfid)) or torch.isinf(torch.tensor(rfid)):
                warnings.warn("The product of covariance of train and test features is out of bounds.")

            eval_score["rFID"] = rfid

        if self._enable_codebook_usage_measure:
            usage = float(len(self._set_of_codebook_indices)) / self._num_codebook_entries
            eval_score["CodebookUsage"] = usage

        if self._enable_codebook_entropy_measure:
            probs = self._codebook_frequencies / self._codebook_frequencies.sum()
            entropy = (-torch.log2(probs + 1e-8) * probs).sum()
            eval_score["CodebookEntropy"] = entropy

        if self._enable_ssim_psnr:
            ssim = self._ssim_total / self._num_examples
            psnr = self._psnr_total / self._num_examples
            eval_score["SSIM"] = ssim
            eval_score["PSNR"] = psnr

        if self._enable_lpips:
            eval_score["LPIPS"] = self._lpips_total / self._num_examples

        return eval_score
    
if __name__ == "__main__":
    from piq import ssim, psnr
    device = torch.device("cuda:4" if torch.cuda.is_available() else "cpu")
    # evaluator = VQGANEvaluator(device=device)

    # test
    img1 = torch.rand(1, 3, 32, 32)
    img2 = img1.clone()
    img3 = img1 + torch.randn_like(img1) * 0.01
    img4 = img1 + torch.randn_like(img1) * 0.05
    img5 = img1 + torch.randn_like(img1) * 0.1
    img6 = 1.0 - img1

    samples = [
        (img1, img2),
        (img1, img3),
        (img1, img4),
        (img1, img5),
        (img1, img6)
    ]

    for i, (real, fake) in enumerate(samples, 1):
        real = torch.clamp(real, 0.0, 1.0)
        fake = torch.clamp(fake, 0.0, 1.0)
        ssim_val = ssim(real, fake, data_range=1.0).item()
        psnr_val = psnr(real, fake, data_range=1.0).item()
