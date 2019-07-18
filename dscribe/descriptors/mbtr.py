# -*- coding: utf-8 -*-
"""Copyright 2019 DScribe developers

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import absolute_import, division, print_function, unicode_literals
from builtins import (bytes, str, open, super, range, zip, round, input, int, pow, object)
import sys
import math
import numpy as np

from scipy.spatial.distance import cdist
from scipy.sparse import lil_matrix, coo_matrix
from scipy.special import erf

from ase import Atoms
import ase.data

from dscribe.core import System
from dscribe.descriptors import Descriptor
from dscribe.libmbtr.mbtrwrapper import MBTRWrapper
import dscribe.utils.geometry

import chronic


class MBTR(Descriptor):
    """Implementation of the Many-body tensor representation up to :math:`k=3`.

    You can choose which terms to include by providing a dictionary in the
    k1, k2 or k3 arguments. This dictionary should contain information
    under three keys: "geometry", "grid" and "weighting". See the examples
    below for how to format these dictionaries.

    You can use this descriptor for finite and periodic systems. When dealing
    with periodic systems or when using machine learning models that use the
    Euclidean norm to measure distance between vectors, it is advisable to use
    some form of normalization.

    For the geometry functions the following choices are available:

    * :math:`k=1`:

       * "atomic_number": The atomic numbers.

    * :math:`k=2`:

       * "distance": Pairwise distance in angstroms.
       * "inverse_distance": Pairwise inverse distance in 1/angstrom.

    * :math:`k=3`:

       * "angle": Angle in degrees.
       * "cosine": Cosine of the angle.

    For the weighting the following functions are available:

    * :math:`k=1`:

       * "unity": No weighting.

    * :math:`k=2`:

       * "unity": No weighting.
       * "exp" or "exponential": Weighting of the form :math:`e^{-sx}`

    * :math:`k=3`:

       * "unity": No weighting.
       * "exp" or "exponential": Weighting of the form :math:`e^{-sx}`

    The exponential weighting is motivated by the exponential decay of screened
    Coulombic interactions in solids. In the exponential weighting the
    parameters **cutoff** determines the value of the weighting function after
    which the rest of the terms will be ignored and the parameter **scale**
    corresponds to :math:`s`. The meaning of :math:`x` changes for different
    terms as follows:

    * :math:`k=2`: :math:`x` = Distance between A->B
    * :math:`k=3`: :math:`x` = Distance from A->B->C->A.

    In the grid setup *min* is the minimum value of the axis, *max* is the
    maximum value of the axis, *sigma* is the standard deviation of the
    gaussian broadening and *n* is the number of points sampled on the
    grid.

    If flatten=False, a list of dense np.ndarrays for each k in ascending order
    is returned. These arrays are of dimension (n_elements x n_elements x
    n_grid_points), where the elements are sorted in ascending order by their
    atomic number.

    If flatten=True, a scipy.sparse.coo_matrix is returned. This sparse matrix
    is of size (1, n_features), where n_features is given by
    get_number_of_features(). This vector is ordered so that the different
    k-terms are ordered in ascending order, and within each k-term the
    distributions at each entry (i, j, h) of the tensor are ordered in an
    ascending order by (i * n_elements) + (j * n_elements) + (h * n_elements).

    This implementation does not support the use of a non-identity correlation
    matrix.
    """
    def __init__(
            self,
            species,
            periodic,
            k1=None,
            k2=None,
            k3=None,
            normalize_gaussians=True,
            normalization="none",
            flatten=True,
            sparse=False
            ):
        """
        Args:
            species (iterable): The chemical species as a list of atomic
                numbers or as a list of chemical symbols. Notice that this is not
                the atomic numbers that are present for an individual system, but
                should contain all the elements that are ever going to be
                encountered when creating the descriptors for a set of systems.
                Keeping the number of chemical speices as low as possible is
                preferable.
            periodic (bool): Determines whether the system is considered to be
                periodic.
            k1 (dict): Setup for the k=1 term. For example::

                k1 = {
                    "geometry": {"function": "atomic_number"},
                    "grid": {"min": 1, "max": 10, "sigma": 0.1, "n": 50}
                }

            k2 (dict): Dictionary containing the setup for the k=2 term.
                Contains setup for the used geometry function, discretization and
                weighting function. For example::

                    k2 = {
                        "geometry": {"function": "inverse_distance"},
                        "grid": {"min": 0.1, "max": 2, "sigma": 0.1, "n": 50},
                        "weighting": {"function": "exp", "scale": 0.75, "cutoff": 1e-2}
                    }

            k3 (dict): Dictionary containing the setup for the k=3 term.
                Contains setup for the used geometry function, discretization and
                weighting function. For example::

                    k3 = {
                        "geometry": {"function": "angle"},
                        "grid": {"min": 0, "max": 180, "sigma": 5, "n": 50},
                        "weighting" = {"function": "exp", "scale": 0.5, "cutoff": 1e-3}
                    }

            normalize_gaussians (bool): Determines whether the gaussians are
                normalized to an area of 1. Defaults to True. If False, the
                normalization factor is dropped and the gaussians have the form.
                :math:`e^{-(x-\mu)^2/2\sigma^2}`
            normalization (str): Determines the method for normalizing the
                output. The available options are:

                * "none": No normalization.
                * "l2_each": Normalize the Euclidean length of each k-term
                  individually to unity.
                * "n_atoms": Normalize the output by dividing it with the number
                  of atoms in the system. If the system is periodic, the number
                  of atoms is determined from the given unit cell.

            flatten (bool): Whether the output should be flattened to a 1D
                array. If False, a dictionary of the different tensors is
                provided, containing the values under keys: "k1", "k2", and
                "k3":
            sparse (bool): Whether the output should be a sparse matrix or a
                dense numpy array.
        """
        if sparse and not flatten:
            raise ValueError(
                "Cannot provide a non-flattened output in sparse output because"
                " only 2D sparse matrices are supported. If you want a "
                "non-flattened output, please specify sparse=False in the MBTR"
                "constructor."
            )
        super().__init__(flatten, sparse)
        self.system = None
        self.k1 = k1
        self.k2 = k2
        self.k3 = k3
        self.periodic = periodic

        # Setup the involved chemical species
        self.species = species

        self.normalization = normalization
        self.normalize_gaussians = normalize_gaussians
        self.is_center_periodic = periodic

        # Initializing .create() level variables
        self._interaction_limit = None
        self._is_local = False
        self._k1_geoms = None
        self._k1_weights = None
        self._k2_geoms = None
        self._k2_weights = None
        self._k3_geoms = None
        self._k3_weights = None
        self._axis_k1 = None
        self._axis_k2 = None
        self._axis_k3 = None

        # Check that weighting function is specified for periodic systems
        if self.periodic:
            if self.k2 is not None:
                valid = False
                weighting = self.k2.get("weighting")
                if weighting is not None:
                    function = weighting.get("function")
                    if function is not None:
                        if function != "unity":
                            valid = True
                if not valid:
                    raise ValueError(
                        "Periodic systems need to have a weighting function."
                    )

            if self.k3 is not None:
                valid = False
                weighting = self.k3.get("weighting")
                if weighting is not None:
                    function = weighting.get("function")
                    if function is not None:
                        if function != "unity":
                            valid = True

                if not valid:
                    raise ValueError(
                        "Periodic systems need to have a weighting function."
                    )

    @property
    def k1(self):
        return self._k1

    @k1.setter
    def k1(self, value):
        if value is not None:

            # Check that only valid keys are used in the setups
            for key in value.keys():
                valid_keys = set(("geometry", "grid", "weighting"))
                if key not in valid_keys:
                    raise ValueError("The given setup contains the following invalid key: {}".format(key))

            # Check the geometry function
            geom_func = value["geometry"].get("function")
            if geom_func is not None:
                valid_geom_func = set(("atomic_number",))
                if geom_func not in valid_geom_func:
                    raise ValueError(
                        "Unknown geometry function specified for k=1. Please use one of"
                        " the following: {}".format(valid_geom_func)
                    )

            # Check the weighting function
            weighting = value.get("weighting")
            if weighting is not None:
                valid_weight_func = set(("unity",))
                weight_func = weighting.get("function")
                if weight_func not in valid_weight_func:
                    raise ValueError(
                        "Unknown weighting function specified for k=1. Please use one of"
                        " the following: {}".format(valid_weight_func)
                    )

            # Check grid
            self.check_grid(value["grid"])
        self._k1 = value

    @property
    def k2(self):
        return self._k2

    @k2.setter
    def k2(self, value):
        if value is not None:

            # Check that only valid keys are used in the setups
            for key in value.keys():
                valid_keys = set(("geometry", "grid", "weighting"))
                if key not in valid_keys:
                    raise ValueError("The given setup contains the following invalid key: {}".format(key))

            # Check the geometry function
            geom_func = value["geometry"].get("function")
            if geom_func is not None:
                valid_geom_func = set(("distance", "inverse_distance"))
                if geom_func not in valid_geom_func:
                    raise ValueError(
                        "Unknown geometry function specified for k=2. Please use one of"
                        " the following: {}".format(valid_geom_func)
                    )

            # Check the weighting function
            weighting = value.get("weighting")
            if weighting is not None:
                valid_weight_func = set(("unity", "exponential", "exp"))
                weight_func = weighting.get("function")
                if weight_func not in valid_weight_func:
                    raise ValueError(
                        "Unknown weighting function specified for k=2. Please use one of"
                        " the following: {}".format(valid_weight_func)
                    )
                else:
                    if weight_func == "exponential" or weight_func == "exp":
                        needed = ("cutoff", "scale")
                        for pname in needed:
                            param = weighting.get(pname)
                            if param is None:
                                raise ValueError(
                                    "Missing value for '{}' in the k=2 weighting.".format(key)
                                )

            # Check grid
            self.check_grid(value["grid"])
        self._k2 = value

    @property
    def k3(self):
        return self._k3

    @k3.setter
    def k3(self, value):
        if value is not None:

            # Check that only valid keys are used in the setups
            for key in value.keys():
                valid_keys = set(("geometry", "grid", "weighting"))
                if key not in valid_keys:
                    raise ValueError("The given setup contains the following invalid key: {}".format(key))

            # Check the geometry function
            geom_func = value["geometry"].get("function")
            if geom_func is not None:
                valid_geom_func = set(("angle", "cosine"))
                if geom_func not in valid_geom_func:
                    raise ValueError(
                        "Unknown geometry function specified for k=2. Please use one of"
                        " the following: {}".format(valid_geom_func)
                    )

            # Check the weighting function
            weighting = value.get("weighting")
            if weighting is not None:
                valid_weight_func = set(("unity", "exponential", "exp"))
                weight_func = weighting.get("function")
                if weight_func not in valid_weight_func:
                    raise ValueError(
                        "Unknown weighting function specified for k=2. Please use one of"
                        " the following: {}".format(valid_weight_func)
                    )
                else:
                    if weight_func == "exponential" or weight_func == "exp":
                        needed = ("cutoff", "scale")
                        for pname in needed:
                            param = weighting.get(pname)
                            if param is None:
                                raise ValueError(
                                    "Missing value for '{}' in the k=3 weighting.".format(key)
                                )

            # Check grid
            self.check_grid(value["grid"])
        self._k3 = value

    @property
    def species(self):
        return self._species

    @species.setter
    def species(self, value):
        """Used to check the validity of given atomic numbers and to initialize
        the C-memory layout for them.

        Args:
            value(iterable): Chemical species either as a list of atomic
                numbers or list of chemical symbols.
        """
        # The species are stored as atomic numbers for internal use.
        self._set_species(value)

        # Setup mappings between atom indices and types together with some
        # statistics
        self.atomic_number_to_index = {}
        self.index_to_atomic_number = {}
        for i_atom, atomic_number in enumerate(self._atomic_numbers):
            self.atomic_number_to_index[atomic_number] = i_atom
            self.index_to_atomic_number[i_atom] = atomic_number
        self.n_elements = len(self._atomic_numbers)
        self.max_atomic_number = max(self._atomic_numbers)
        self.min_atomic_number = min(self._atomic_numbers)

    @property
    def normalization(self):
        return self._normalization

    @normalization.setter
    def normalization(self, value):
        """Checks that the given normalization is valid.

        Args:
            value(str): The normalization method to use.
        """
        norm_options = set(("l2_each", "none", "n_atoms"))
        if value not in norm_options:
            raise ValueError(
                "Unknown normalization option given. Please use one of the "
                "following: {}.".format(", ".join([str(x) for x in norm_options]))
            )
        self._normalization = value

    def create(self, system, n_jobs=1, verbose=False):
        """Return MBTR output for the given systems.

        Args:
            system (:class:`ase.Atoms` or list of :class:`ase.Atoms`): One or many atomic structures.
            n_jobs (int): Number of parallel jobs to instantiate. Parallellizes
                the calculation across samples. Defaults to serial calculation
                with n_jobs=1.
            verbose(bool): Controls whether to print the progress of each job
                into to the console.

        Returns:
            np.ndarray | scipy.sparse.csr_matrix | list: MBTR for the
            given systems. The return type depends on the 'sparse' and
            'flatten'-attributes. For flattened output a single numpy array or
            sparse scipy.csr_matrix is returned. The first dimension is
            determined by the amount of systems. If the output is not
            flattened, dictionaries containing the MBTR tensors for each k-term
            are returned.
        """
        # If single system given, skip the parallelization
        if isinstance(system, (Atoms, System)):
            return self.create_single(system)

        # Combine input arguments
        inp = [(i_sys,) for i_sys in system]

        # Here we precalculate the size for each job to preallocate memory.
        if self.flatten:
            n_samples = len(system)
            k, m = divmod(n_samples, n_jobs)
            jobs = (inp[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n_jobs))
            output_sizes = [len(job) for job in jobs]
        else:
            output_sizes = None

        # Create in parallel
        output = self.create_parallel(inp, self.create_single, n_jobs, output_sizes, verbose=verbose)

        return output

    def create_single(self, system):
        """Return the many-body tensor representation for the given system.

        Args:
            system (:class:`ase.Atoms` | :class:`.System`): Input system.

        Returns:
            dict | np.ndarray | scipy.sparse.coo_matrix: The return type is
            specified by the 'flatten' and 'sparse'-parameters. If the output
            is not flattened, a dictionary containing of MBTR outputs as numpy
            arrays is created. Each output is under a "kX" key. If the output
            is flattened, a single concatenated output vector is returned,
            either as a sparse or a dense vector.
       """
        # Transform the input system into the internal System-object
        system = self.get_system(system)

        # Initializes the scalar numbers that depend no the system
        with chronic.Timer("Scalars"):
            self.initialize_scalars(system)

        # Create output with the currently set grid
        with chronic.Timer("Grid"):
            grid = {}
            if self.k1 is not None:
                grid["k1"] = self.k1["grid"]
            if self.k2 is not None:
                grid["k2"] = self.k2["grid"]
            if self.k3 is not None:
                grid["k3"] = self.k3["grid"]
            output = self.create_with_grid(grid)
        return output

    def create_with_grid(self, grid):
        """Used to recalculate MBTR for an already seen system but with
        different grid setttings. This function can be used after the scalar
        values have been initialized with "initialize_scalars".
        """
        for value in grid.values():
            self.check_grid(value)

        mbtr = {}
        if self.k1 is not None:
            k1 = self.get_k1_convolution(grid["k1"])
            mbtr["k1"] = k1

        if self.k2 is not None:
            k2 = self.get_k2_convolution(grid["k2"])
            mbtr["k2"] = k2

        if self.k3 is not None:
            k3 = self.get_k3_convolution(grid["k3"])
            mbtr["k3"] = k3

        # Handle normalization
        if self.normalization == "l2_each":
            if self.flatten is True:
                for key, value in mbtr.items():
                    i_data = np.array(value.tocsr().data)
                    i_norm = np.linalg.norm(i_data)
                    mbtr[key] = value/i_norm
            else:
                for key, value in mbtr.items():
                    i_data = value.ravel()
                    i_norm = np.linalg.norm(i_data)
                    mbtr[key] = value/i_norm
        elif self.normalization == "n_atoms":
            n_atoms = len(self.system)
            if self.flatten is True:
                for key, value in mbtr.items():
                    mbtr[key] = value/n_atoms
            else:
                for key, value in mbtr.items():
                    mbtr[key] = value/n_atoms

        # Flatten output if requested
        if self.flatten:
            length = 0

            datas = []
            rows = []
            cols = []
            for key in sorted(mbtr.keys()):
                tensor = mbtr[key]
                size = tensor.shape[1]
                coo = tensor.tocoo()
                datas.append(coo.data)
                rows.append(coo.row)
                cols.append(coo.col + length)
                length += size

            datas = np.concatenate(datas)
            rows = np.concatenate(rows)
            cols = np.concatenate(cols)
            mbtr = coo_matrix((datas, (rows, cols)), shape=[1, length], dtype=np.float32)

            # Make into a dense array if requested
            if not self.sparse:
                mbtr = mbtr.toarray()

        return mbtr

    def initialize_scalars(self, system):
        """Used to initialize the scalar values for each k-term.
        """
        # Transform the input system into the internal System-object
        system = self.get_system(system)

        # Ensuring variables are re-initialized when a new system is introduced
        self._interaction_limit = None
        self.system = system
        self._k1_geoms = None
        self._k1_weights = None
        self._k2_geoms = None
        self._k2_weights = None
        self._k3_geoms = None
        self._k3_weights = None
        self._axis_k1 = None
        self._axis_k2 = None
        self._axis_k3 = None

        if self._is_local:
            self._interaction_limit = 1
        else:
            self._interaction_limit = len(system)

        # Check that the system does not have elements that are not in the list
        # of atomic numbers
        self.check_atomic_numbers(system.get_atomic_numbers())

        if self.k1 is not None:
            cell_indices = np.zeros((len(system), 3), dtype=int)
            self.k1_geoms_and_weights(system, cell_indices)
        if self.k2 is not None:
            # If needed, create the extended system
            system_k2 = system
            if self.periodic:
                system_k2, cell_indices = self.create_extended_system(system, 2)
            else:
                cell_indices = np.zeros((len(system), 3), dtype=int)
            self.k2_geoms_and_weights(system_k2, cell_indices)

            # Free memory
            system_k2 = None

        if self.k3 is not None:
            # If needed, create the extended system
            system_k3 = system
            if self.periodic:
                system_k3, cell_indices = self.create_extended_system(system, 3)
            else:
                cell_indices = np.zeros((len(system), 3), dtype=int)
            self.k3_geoms_and_weights(system_k3, cell_indices)

            # Free memory
            system_k3 = None

    def get_number_of_features(self):
        """Used to inquire the final number of features that this descriptor
        will have.

        Returns:
            int: Number of features for this descriptor.
        """
        n_features = 0
        n_elem = self.n_elements

        if self.k1 is not None:
            n_k1_grid = self.k1["grid"]["n"]
            n_k1 = n_elem*n_k1_grid
            n_features += n_k1
        if self.k2 is not None:
            n_k2_grid = self.k2["grid"]["n"]
            n_k2 = (n_elem*(n_elem+1)/2)*n_k2_grid
            n_features += n_k2
        if self.k3 is not None:
            n_k3_grid = self.k3["grid"]["n"]
            n_k3 = (n_elem*n_elem*(n_elem+1)/2)*n_k3_grid
            n_features += n_k3

        return int(n_features)

    def get_location(self, species):
        """Can be used to query the location of a species combination in the
        the flattened output.

        Args:
            species(tuple): A tuple containing a species combination as
            chemical symbols or atomic numbers. The tuple can be for example
            ("H"), ("H", "O") or ("H", "O", "H").

        Returns:
            slice: slice containing the location of the specified species
            combination. The location is given as a python slice-object, that
            can be directly used to target ranges in the output.

        Raises:
            ValueError: If the requested species combination is not in the
            output or if invalid species defined.
        """
        # Check that the corresponding part is calculated
        k = len(species)
        term = getattr(self, "k{}".format(k))
        if term is None:
            raise ValueError(
                "Cannot retrieve the location for {}, as the term {} has not "
                "been specied.".format(species, term)
            )

        # Change chemical elements into atomic numbers
        numbers = []
        for specie in species:
            if isinstance(specie, str):
                try:
                    specie = ase.data.atomic_numbers[specie]
                except KeyError:
                    raise ValueError("Invalid chemical species: {}".format(specie))
            numbers.append(specie)

        # Change into internal indexing
        numbers = [self.atomic_number_to_index[x] for x in numbers]
        n_elem = self.n_elements

        # k=1
        if len(numbers) == 1:
            n1 = self.k1["grid"]["n"]
            i = numbers[0]
            m = i
            start = int(m*n1)
            end = int((m+1)*n1)

        # k=2
        if len(numbers) == 2:
            if numbers[0] > numbers[1]:
                numbers = list(reversed(numbers))

            n2 = self.k2["grid"]["n"]
            i = numbers[0]
            j = numbers[1]

            # This is the index of the spectrum. It is given by enumerating the
            # elements of an upper triangular matrix from left to right and top
            # to bottom.
            m = j + i*n_elem - i*(i+1)/2

            offset = 0
            if self.k1 is not None:
                n1 = self.k1["grid"]["n"]
                offset += n_elem*n1
            start = int(offset+m*n2)
            end = int(offset+(m+1)*n2)

        # k=3
        if len(numbers) == 3:
            if numbers[0] > numbers[2]:
                numbers = list(reversed(numbers))

            n3 = self.k3["grid"]["n"]
            i = numbers[0]
            j = numbers[1]
            k = numbers[2]

            # This is the index of the spectrum. It is given by enumerating the
            # elements of a three-dimensional array where for valid elements
            # k>=i. The enumeration begins from [0, 0, 0], and ends at [n_elem,
            # n_elem, n_elem], looping the elements in the order k, i, j.
            m = j*n_elem*(n_elem+1)/2 + k + i*n_elem - i*(i+1)/2

            offset = 0
            if self.k1 is not None:
                n1 = self.k1["grid"]["n"]
                offset += n_elem*n1
            if self.k2 is not None:
                n2 = self.k2["grid"]["n"]
                offset += (n_elem*(n_elem+1)/2)*n2
            start = int(offset+m*n3)
            end = int(offset+(m+1)*n3)

        return slice(start, end)

    def create_extended_system(self, primitive_system, term_number):
        """Used to create a periodically extended system, that is as small as
        possible by rejecting atoms for which the given weighting will be below
        the given threshold.

        Modified for the local MBTR to only consider distances from the central
        atom and to enable taking the virtual sites into account.

        Args:
            primitive_system (System): The original primitive system to
                duplicate.
            term_number (int): The term number of the tensor. For k=2, the max
                distance is x, for k>2, the distance is given by 2*x.

        Returns:
            tuple: Tuple containing the new extended system as the first entry
            and the index of the periodically repeated cell for each atom as
            the second entry. The extended system is determined is extended so that each atom can at most
            have a weight that is larger or equivalent to the given threshold.
        """
        # We need to specify that the relative positions should not be wrapped.
        # Otherwise the repeated systems may overlap with the positions taken
        # with get_positions()
        relative_pos = np.array(primitive_system.get_scaled_positions(wrap=False))
        numbers = np.array(primitive_system.numbers)
        cartesian_pos = np.array(primitive_system.get_positions())
        cell = np.array(primitive_system.get_cell())

        # Determine the upper limit of how many copies we need in each cell
        # vector direction. We take as many copies as needed for the
        # exponential weight to come down to the given threshold.
        cell_vector_lengths = np.linalg.norm(cell, axis=1)
        n_copies_axis = np.zeros(3, dtype=int)
        weighting = getattr(self, "k{}".format(term_number))["weighting"]
        weighting_function = weighting["function"]
        cutoff = weighting["cutoff"]

        if weighting_function == "exponential" or weighting_function == "exp":
            scale = weighting["scale"]
            function = lambda x: np.exp(-scale*x)

        for i_axis, axis_length in enumerate(cell_vector_lengths):
            limit_found = False
            n_copies = -1
            while (not limit_found):
                n_copies += 1
                distance = n_copies*cell_vector_lengths[i_axis]

                # For terms k>2 we double the distances to take into
                # account the "loop" that is required.
                if term_number > 2:
                    distance = 2*distance

                weight = function(distance)
                if weight < cutoff:
                    n_copies_axis[i_axis] = n_copies
                    limit_found = True

        # Create copies of the cell but keep track of the atoms in the
        # original cell
        num_extended = []
        pos_extended = []
        num_extended.append(numbers)
        pos_extended.append(cartesian_pos)
        a = np.array([1, 0, 0])
        b = np.array([0, 1, 0])
        c = np.array([0, 0, 1])
        cell_indices = [np.zeros((len(primitive_system), 3), dtype=int)]

        for i in range(-n_copies_axis[0], n_copies_axis[0]+1):
            for j in range(-n_copies_axis[1], n_copies_axis[1]+1):
                for k in range(-n_copies_axis[2], n_copies_axis[2]+1):
                    if i == 0 and j == 0 and k == 0:
                        continue

                    # Calculate the positions of the copied atoms and filter
                    # out the atoms that are farther away than the given
                    # cutoff.

                    # If the given position is virtual and does not correspond
                    # to a physical atom, the position is not repeated in the
                    # copies.
                    if not self.is_center_periodic and self._interaction_limit == 1:
                        num_copy = np.array(numbers)[1:]
                        pos_copy = np.array(relative_pos)[1:]

                    # If the given position is not virtual and corresponds to
                    # an actual physical atom, the ghost atom is repeated in
                    # the extended system.
                    else:
                        num_copy = np.array(numbers)
                        pos_copy = np.array(relative_pos)

                    pos_shifted = pos_copy-i*a-j*b-k*c
                    pos_copy_cartesian = np.dot(pos_shifted, cell)

                    # Only distances to the atoms within the interaction limit
                    # are considered.
                    positions_to_consider = cartesian_pos[0:self._interaction_limit]
                    distances = cdist(pos_copy_cartesian, positions_to_consider)

                    # For terms above k==2 we double the distances to take into
                    # account the "loop" that is required.
                    if term_number > 2:
                        distances *= 2

                    weights = function(distances)
                    weight_mask = weights >= cutoff

                    # Create a boolean mask that says if the atom is within the
                    # range from at least one atom in the original cell
                    valids_mask = np.any(weight_mask, axis=1)

                    if np.any(valids_mask):
                        valid_pos = pos_copy_cartesian[valids_mask]
                        valid_num = num_copy[valids_mask]
                        valid_ind = np.tile(np.array([i, j, k], dtype=int), (len(valid_num), 1))

                        pos_extended.append(valid_pos)
                        num_extended.append(valid_num)
                        cell_indices.append(valid_ind)

        pos_extended = np.concatenate(pos_extended)
        num_extended = np.concatenate(num_extended)
        cell_indices = np.vstack(cell_indices)

        extended_system = System(
            positions=pos_extended,
            numbers=num_extended,
            cell=cell,
            pbc=False
        )

        return extended_system, cell_indices

    def gaussian_sum(self, centers, weights, settings):
        """Calculates a discrete version of a sum of Gaussian distributions.

        The calculation is done through the cumulative distribution function
        that is better at keeping the integral of the probability function
        constant with coarser grids.

        The values are normalized by dividing with the maximum value of a
        gaussian with the given standard deviation.

        Args:
            centers (1D np.ndarray): The means of the gaussians.
            weights (1D np.ndarray): The weights for the gaussians.
            settings (dict): The grid settings

        Returns:
            Value of the gaussian sums on the given grid.
        """
        start = settings["min"]
        stop = settings["max"]
        sigma = settings["sigma"]
        n = settings["n"]

        dx = (stop - start)/(n-1)
        x = np.linspace(start-dx/2, stop+dx/2, n+1)
        pos = x[np.newaxis, :] - centers[:, np.newaxis]
        y = weights[:, np.newaxis]*1/2*(1 + erf(pos/(sigma*np.sqrt(2))))
        f = np.sum(y, axis=0)

        if not self.normalize_gaussians:
            max_val = 1/(sigma*math.sqrt(2*math.pi))
            f /= max_val

        f_rolled = np.roll(f, -1)
        pdf = (f_rolled - f)[0:-1]/dx  # PDF is the derivative of CDF

        return pdf

    def k1_geoms_and_weights(self, system, cell_indices):
        """Calculate the atom count for each element.

        Args:
            system (System): The atomic system.
            cell_indices (np.ndarray): The cell indices for each atom.

        Returns:
            1D ndarray: The counts for each element in a list where the index
            of atomic number x is self.atomic_number_to_index[x]
        """
        if self._k1_geoms is None or self._k1_weights is None:

            cmbtr = MBTRWrapper(
                system.get_positions(),
                system.get_atomic_numbers(),
                self.atomic_number_to_index,
                interaction_limit=self._interaction_limit,
                indices=cell_indices,
                is_local=self._is_local
            )

            # For k=1, the geometry function is given by the atomic number, and
            # the weighting function is unity by default.
            parameters = {}

            geom_func_name = self.k1["geometry"]["function"]
            if geom_func_name is None:
                geom_func_name = "atomic_numbers"

            self._k1_geoms, self._k1_weights = cmbtr.get_k1_geoms_and_weights(
                geom_func=geom_func_name.encode(),
                weight_func=b"unity",
                parameters=parameters
            )
        return self._k1_geoms, self._k1_weights

    def k2_geoms_and_weights(self, system, cell_indices):
        """Calculates the value of the geometry function and corresponding
        weights for unique two-body combinations.

        Args:
            system (System): The atomic system.

        Returns:
            dict: Inverse distances in the form: {(i, j): [list of angles] }.
            The dictionaries are filled so that the entry for pair i and j is
            in the entry where j>=i.
        """
        if self._k2_geoms is None or self._k2_weights is None:

            cmbtr = MBTRWrapper(
                system.get_positions(),
                system.get_atomic_numbers(),
                self.atomic_number_to_index,
                interaction_limit=self._interaction_limit,
                indices=cell_indices,
                is_local=self._is_local
            )

            # Determine the weighting function and possible radial cutoff
            radial_cutoff = None
            weighting = self.k2.get("weighting")
            parameters = {}
            if weighting is not None:
                weighting_function = weighting["function"]
                if weighting_function == "exponential" or weighting_function == "exp":
                    radial_cutoff = -math.log(weighting["cutoff"])/weighting["scale"]
                    parameters = {
                        b"scale": weighting["scale"],
                        b"cutoff": weighting["cutoff"]
                    }
            else:
                weighting_function = "unity"

            # Determine the geometry function
            geom_func_name = self.k2["geometry"]["function"]

            # If radial cutoff is finite, use it to calculate the sparse
            # distance matrix to reduce computational complexity from O(n^2) to
            # O(n log(n))
            n_atoms = len(system)
            if radial_cutoff is not None:
                dmat = system.get_distance_matrix_within_radius(radial_cutoff, "coo_matrix")
                adj_list = dscribe.utils.geometry.get_adjacency_list(dmat)
                dmat_dense = np.full((n_atoms, n_atoms), sys.float_info.max)  # The non-neighbor values are treated as "infinitely far".
                dmat_dense[dmat.col, dmat.row] = dmat.data
            # If no weighting is used, the full distance matrix is calculated
            else:
                dmat_dense = system.get_distance_matrix()
                adj_list = np.tile(np.arange(n_atoms), (n_atoms, 1))

            self._k2_geoms, self._k2_weights = cmbtr.get_k2_geoms_and_weights(
                distances=dmat_dense,
                neighbours=adj_list,
                geom_func=geom_func_name.encode(),
                weight_func=weighting_function.encode(),
                parameters=parameters
            )
        return self._k2_geoms, self._k2_weights

    def k3_geoms_and_weights(self, system, cell_indices):
        """Calculates the value of the geometry function and corresponding
        weights for unique three-body combinations.

        Args:
            system (System): The atomic system.

        Returns:
            tuple: (geoms, weights) Cosines of the angles (values between -1
            and 1) in the form {(i,j,k): [list of angles] }. The weights
            corresponding to the angles are stored in a similar dictionary.
        """
        if self._k3_geoms is None or self._k2_weights is None:

            # Calculate the angles with the C++ implementation
            cmbtr = MBTRWrapper(
                system.get_positions(),
                system.get_atomic_numbers(),
                self.atomic_number_to_index,
                interaction_limit=self._interaction_limit,
                indices=cell_indices,
                is_local=self._is_local
            )

            # Determine the weighting function and possible radial cutoff
            radial_cutoff = None
            weighting = self.k3.get("weighting")
            parameters = {}
            if weighting is not None:
                weighting_function = weighting["function"]
                if weighting_function == "exponential" or weighting_function == "exp":
                    radial_cutoff = -0.5*math.log(weighting["cutoff"])/weighting["scale"]
                    parameters = {
                        b"scale": weighting["scale"],
                        b"cutoff": weighting["cutoff"]
                    }
            else:
                weighting_function = "unity"

            # Determine the geometry function
            geom_func_name = self.k3["geometry"]["function"]

            # If radial cutoff is finite, use it to calculate the sparse
            # distance matrix to reduce computational complexity from O(n^2) to
            # O(n log(n))
            n_atoms = len(system)
            if radial_cutoff is not None:
                dmat = system.get_distance_matrix_within_radius(radial_cutoff, "coo_matrix")
                adj_list = dscribe.utils.geometry.get_adjacency_list(dmat)
                dmat_dense = np.full((n_atoms, n_atoms), sys.float_info.max)  # The non-neighbor values are treated as "infinitely far".
                dmat_dense[dmat.col, dmat.row] = dmat.data
            # If no weighting is used, the full distance matrix is calculated
            else:
                dmat_dense = system.get_distance_matrix()
                adj_list = np.tile(np.arange(n_atoms), (n_atoms, 1))

            self._k3_geoms, self._k3_weights = cmbtr.get_k3_geoms_and_weights(
                distances=dmat_dense,
                neighbours=adj_list,
                geom_func=geom_func_name.encode(),
                weight_func=weighting_function.encode(),
                parameters=parameters
            )

        return self._k3_geoms, self._k3_weights

    def get_k1_convolution(self, grid):
        """Calculates the first order terms where the scalar mapping is the
        number of atoms of a certain type.

        Args:
            grid (dict): Grid settings.

        Returns:
            ndarray | scipy.sparse.lil_matrix: K1 values.
        """
        start = grid["min"]
        stop = grid["max"]
        n = grid["n"]
        self._axis_k1 = np.linspace(start, stop, n)

        n_elem = self.n_elements
        k1_geoms, k1_weights = self._k1_geoms, self._k1_weights

        # Depending of flattening, use either a sparse matrix or a dense one.
        if self.flatten:
            k1 = lil_matrix((1, n_elem*n), dtype=np.float32)
        else:
            k1 = np.zeros((n_elem, n), dtype=np.float32)

        for key in k1_geoms.keys():
            i = key[0]

            geoms = np.array(k1_geoms[key])
            weights = np.array(k1_weights[key])

            # Broaden with a gaussian
            gaussian_sum = self.gaussian_sum(geoms, weights, grid)

            if self.flatten:
                start = i*n
                end = (i+1)*n
                k1[0, start:end] = gaussian_sum
            else:
                k1[i, :] = gaussian_sum

        return k1

    def get_k2_convolution(self, grid):
        """Calculates the second order terms where the scalar mapping is the
        inverse distance between atoms.

        Args:
            grid (dict): The grid settings

        Returns:
            1D ndarray: flattened K2 values.
        """
        start = grid["min"]
        stop = grid["max"]
        n = grid["n"]
        self._axis_k2 = np.linspace(start, stop, n)

        k2_geoms, k2_weights = self._k2_geoms, self._k2_weights
        n_elem = self.n_elements

        # Depending of flattening, use either a sparse matrix or a dense one.
        if self.flatten:
            k2 = lil_matrix(
                (1, int(n_elem*(n_elem+1)/2*n)), dtype=np.float32)
        else:
            k2 = np.zeros((self.n_elements, self.n_elements, n), dtype=np.float32)

        for key in k2_geoms.keys():
            i = key[0]
            j = key[1]

            # This is the index of the spectrum. It is given by enumerating the
            # elements of an upper triangular matrix from left to right and top
            # to bottom.
            m = int(j + i*n_elem - i*(i+1)/2)

            geoms = np.array(k2_geoms[key])
            weights = np.array(k2_weights[key])

            # Broaden with a gaussian
            gaussian_sum = self.gaussian_sum(geoms, weights, grid)

            if self.flatten:
                start = m*n
                end = (m + 1)*n
                k2[0, start:end] = gaussian_sum
            else:
                k2[i, j, :] = gaussian_sum

        return k2

    def get_k3_convolution(self, grid):
        """Calculates the third order terms where the scalar mapping is the
        angle between 3 atoms.

        Args:
            grid (dict): The grid settings

        Returns:
            1D ndarray: flattened K3 values.
        """
        start = grid["min"]
        stop = grid["max"]
        n = grid["n"]
        self._axis_k3 = np.linspace(start, stop, n)

        k3_geoms, k3_weights = self._k3_geoms, self._k3_weights
        n_elem = self.n_elements

        # Depending of flattening, use either a sparse matrix or a dense one.
        if self.flatten:
            k3 = lil_matrix(
                (1, int(n_elem*n_elem*(n_elem+1)/2*n)), dtype=np.float32
            )
        else:
            k3 = np.zeros((n_elem, n_elem, n_elem, n), dtype=np.float32)

        for key in k3_geoms.keys():
            i = key[0]
            j = key[1]
            k = key[2]

            # This is the index of the spectrum. It is given by enumerating the
            # elements of a three-dimensional array where for valid elements
            # k>=i. The enumeration begins from [0, 0, 0], and ends at [n_elem,
            # n_elem, n_elem], looping the elements in the order j, i, k.
            m = int(j*n_elem*(n_elem+1)/2 + k + i*n_elem - i*(i+1)/2)

            geoms = np.array(k3_geoms[key])
            weights = np.array(k3_weights[key])

            # Broaden with a gaussian
            gaussian_sum = self.gaussian_sum(geoms, weights, grid)

            if self.flatten:
                start = m*n
                end = (m+1)*n
                k3[0, start:end] = gaussian_sum
            else:
                k3[i, j, k, :] = gaussian_sum

        return k3

    def check_grid(self, grid):
        """Used to ensure that the given grid settings are valid.

        Args:
            grid(dict): Dictionary containing the grid setup.
        """
        msg = "The grid information is missing the value for {}"
        val_names = ["min", "max", "sigma", "n"]
        for val_name in val_names:
            try:
                grid[val_name]
            except Exception:
                raise KeyError(msg.format(val_name))

        # Make the n into integer
        grid["n"] = int(grid["n"])
        if grid["min"] >= grid["max"]:
            raise ValueError(
                "The min value should be smaller than the max value."
            )
