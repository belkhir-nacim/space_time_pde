"""RB2 Experiment Dataloader"""
import os
import torch
from torch.utils.data import Dataset, Sampler
import numpy as np
from scipy.interpolate import RegularGridInterpolator
# pylint: disable=too-manz-arguments, too-manz-instance-attributes, too-manz-locals


class RB2DataLoader(Dataset):
    """Pytorch Dataset instance for loading Rayleigh Bernard 2D dataset.

    Loads a 2d space + time cubic cutout from the whole simulation.
    """
    def __init__(self, data_dir="./", data_filename="rb2d_ra1e6_s42.npz",
                 nx=128, nz=128, nt=16, n_samp_pts_per_crop=1024, interp_method='linear',
                 downsamp_xz=4, downsamp_t=4, normalize_output=False, return_hres=False):
        """

        Initialize DataSet
        Args:
          data_dir: str, path to the dataset folder, default="./"
          data_filename: str, name of the dataset file, default="rb2d_ra1e6_s42"
          nx: int, number of 'pixels' in x dimension for high res dataset.
          nz: int, number of 'pixels' in z dimension for high res dataset.
          nt: int, number of timesteps in time for high res dataset.
          n_samp_pts_per_crop: int, number of sample points to return per crop.
          interp_method: str, interpolation method. choice of 'linear/nearest'
          downsamp_xz: int, downsampling factor for the spatial dimensions.
          downsamp_t: int, downsampling factor for the temporal dimension.
          normalize_output: bool, whether to normalize the range of each channel to [0, 1].
          return_hres: bool, whether to return the high-resolution data.
        """
        self.data_dir = data_dir
        self.data_filename = data_filename
        self.nx_hres = nx
        self.nz_hres = nz
        self.nt_hres = nt
        self.nx_lres = int(nx/downsamp_xz)
        self.nz_lres = int(nz/downsamp_xz)
        self.nt_lres = int(nt/downsamp_t)
        self.n_samp_pts_per_crop = n_samp_pts_per_crop
        self.interp_method = interp_method
        self.downsamp_xz = downsamp_xz
        self.downsamp_t = downsamp_t
        self.normalize_output = normalize_output
        self.return_hres = return_hres

        # concatenating pressure, temperature, x-velocity, and z-velocity as a 4 channel array: pbuw
        # shape: (4, 200, 512, 128)
        npdata = np.load(os.path.join(self.data_dir, self.data_filename))
        self.data = np.stack([npdata['p'], npdata['b'], npdata['u'], npdata['w']], axis=0)
        self.data = self.data.astype(np.float32)
        self.data = self.data.transpose(0, 1, 3, 2)  # [c, t, z, x]
        nc_data, nt_data, nz_data, nx_data = self.data.shape

        # assert nx, nz, nt are viable
        if (nx > nx_data) or (nz > nz_data) or (nt > nt_data):
            raise ValueError('Resolution in each spatial temporal dimension x ({}), z({}), t({})'
                             'must not exceed dataset limits x ({}) z ({}) t ({})'.format(
                                 nx, nz, nt, nx_data, nz_data, nt_data))
        if (nt % downsamp_t != 0) or (nx % downsamp_xz != 0) or (nz % downsamp_xz != 0):
            raise ValueError('nx, nz and nt must be divisible by downsamp factor.')

        self.nx_start_range = np.arange(0, nx_data-nx+1)
        self.nz_start_range = np.arange(0, nz_data-nz+1)
        self.nt_start_range = np.arange(0, nt_data-nt+1)
        self.rand_grid = np.stack(np.meshgrid(self.nt_start_range,
                                              self.nz_start_range,
                                              self.nx_start_range, indexing='ij'), axis=-1)
        # (xaug, zaug, taug, 3)
        self.rand_start_id = self.rand_grid.reshape([-1, 3])
        self.scale_hres = np.array([self.nt_hres, self.nz_hres, self.nx_hres], dtype=np.int32)
        self.scale_lres = np.array([self.nt_lres, self.nz_lres, self.nx_lres], dtype=np.int32)

        # compute channel-wise mean and std
        self._mean = np.mean(self.data, axis=(1, 2, 3))
        self._std = np.std(self.data, axis=(1, 2, 3))

    def __len__(self):
        return self.rand_start_id.shape[0]

    def __getitem__(self, idx):
        """Get the random cutout data cube corresponding to idx.

        Args:
          idx: int, index of the crop to return. must be smaller than len(self).

        Returns:
          space_time_crop_hres (*optional): array of shape [4, nt_lres, nz_lres, nx_lres],
          where 4 are the phys channels pbuw.
          space_time_crop_lres: array of shape [4, nt_lres, nz_lres, nx_lres], where 4 are the phys
          channels pbuw.
          point_coord: array of shape [n_samp_pts_per_crop, 3], where 3 are the t, x, z dims.
          point_value: array of shape [n_samp_pts_per_crop, 4], where 4 are the phys channels pbuw.
        """
        t_id, z_id, x_id = self.rand_start_id[idx]
        space_time_crop_hres = self.data[:,
                                         t_id:t_id+self.nt_hres,
                                         z_id:z_id+self.nz_hres,
                                         x_id:x_id+self.nx_hres]  # [c, t, z, x]

        # create low res grid from hires space time crop
        interp = RegularGridInterpolator(
            (np.arange(self.nt_hres), np.arange(self.nz_hres), np.arange(self.nx_hres)),
            values=space_time_crop_hres.transpose(1, 2, 3, 0), method=self.interp_method)

        lres_coord = np.stack(np.meshgrid(np.linspace(0, self.nt_hres-1, self.nt_lres),
                                          np.linspace(0, self.nz_hres-1, self.nz_lres),
                                          np.linspace(0, self.nx_hres-1, self.nx_lres),
                                          indexing='ij'), axis=-1)
        space_time_crop_lres = interp(lres_coord).transpose(3, 0, 1, 2)  # [c, t, z, x]

        # create random point samples within space time crop
        point_coord = np.random.rand(self.n_samp_pts_per_crop, 3) * (self.scale_hres - 1)
        point_value = interp(point_coord)
        point_coord = point_coord / (self.scale_hres - 1)

        if self.normalize_output:
            space_time_crop_lres = self.normalize_grid(space_time_crop_lres)
            point_value = self.normalize_points(point_value)

        return_tensors = [space_time_crop_lres, point_coord, point_value]

        # cast everything to float32
        return_tensors = [t.astype(np.float32) for t in return_tensors]

        if self.return_hres:
            return_tensors = [space_time_crop_hres] + return_tensors
        return tuple(return_tensors)

    @property
    def channel_mean(self):
        """channel-wise mean of dataset."""
        return self._mean

    @property
    def channel_std(self):
        """channel-wise mean of dataset."""
        return self._std

    @staticmethod
    def _normalize_array(array, mean, std):
        """normalize array (np or torch)."""
        if isinstance(array, torch.Tensor):
            dev = array.device
            std = torch.tensor(std, device=dev)
            mean = torch.tensor(mean, device=dev)
        return (array - mean) / std

    @staticmethod
    def _denormalize_array(array, mean, std):
        """normalize array (np or torch)."""
        if isinstance(array, torch.Tensor):
            dev = array.device
            std = torch.tensor(std, device=dev)
            mean = torch.tensor(mean, device=dev)
        return array * std + mean

    def normalize_grid(self, grid):
        """Normalize grid.

        Args:
          grid: np array or torch tensor of shape [4, ...], 4 are the num. of phys channels.
        Returns:
          channel normalized grid of same shape as input.
        """
        # reshape mean and std to be broadcastable.
        g_dim = len(grid.shape)
        mean_bc = self.channel_mean[(...,)+(None,)*(g_dim-1)]  # unsqueeze from the back
        std_bc = self.channel_std[(...,)+(None,)*(g_dim-1)]  # unsqueeze from the back
        return self._normalize_array(grid, mean_bc, std_bc)


    def normalize_points(self, points):
        """Normalize points.

        Args:
          points: np array or torch tensor of shape [..., 4], 4 are the num. of phys channels.
        Returns:
          channel normalized points of same shape as input.
        """
        # reshape mean and std to be broadcastable.
        g_dim = len(points.shape)
        mean_bc = self.channel_mean[(None,)*(g_dim-1)]  # unsqueeze from the front
        std_bc = self.channel_std[(None,)*(g_dim-1)]  # unsqueeze from the front
        return self._normalize_array(points, mean_bc, std_bc)

    def denormalize_grid(self, grid):
        """Denormalize grid.

        Args:
          grid: np array or torch tensor of shape [4, ...], 4 are the num. of phys channels.
        Returns:
          channel denormalized grid of same shape as input.
        """
        # reshape mean and std to be broadcastable.
        g_dim = len(grid.shape)
        mean_bc = self.channel_mean[(...,)+(None,)*(g_dim-1)]  # unsqueeze from the back
        std_bc = self.channel_std[(...,)+(None,)*(g_dim-1)]  # unsqueeze from the back
        return self._denormalize_array(grid, mean_bc, std_bc)


    def denormalize_points(self, points):
        """Denormalize points.

        Args:
          points: np array or torch tensor of shape [..., 4], 4 are the num. of phys channels.
        Returns:
          channel denormalized points of same shape as input.
        """
        # reshape mean and std to be broadcastable.
        g_dim = len(points.shape)
        mean_bc = self.channel_mean[(None,)*(g_dim-1)]  # unsqueeze from the front
        std_bc = self.channel_std[(None,)*(g_dim-1)]  # unsqueeze from the front
        return self._denormalize_array(points, mean_bc, std_bc)


if __name__ == '__main__':
    ### example for using the data loader
    data_loader = RB2DataLoader(nt=4, n_samp_pts_per_crop=10000, downsamp_t=2)
    # lres_crop, point_coord, point_value = data_loader[61234]
    # import matplotlib.pyplot as plt
    # plt.scatter(point_coord[:, 1], point_coord[:, 2], c=point_value[:, 0])
    # plt.colorbar()
    # plt.show()
    # plt.imshow(lres_crop[0, :, :, 0].T, origin='lower'); plt.show()
    # plt.imshow(lres_crop[1, :, :, 0].T, origin='lower'); plt.show()

    data_batches = torch.utils.data.DataLoader(
        data_loader, batch_size=16, shuffle=True, num_workers=1)

    for batch_idx, (lowres_input_batch, point_coords, point_values) in enumerate(data_batches):
        print("Reading batch #{}:\t with lowres inputs of size {}, sample coord of size {}, sampe val of size {}"
              .format(batch_idx+1, list(lowres_input_batch.shape),  list(point_coords.shape), list(point_values.shape)))
        if batch_idx > 16:
            break
